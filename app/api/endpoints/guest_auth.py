"""Endpoint Just-in-Time (JIT) Guest Access Approval via Hermes AI Agent & Approval URL."""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from uuid import UUID

import requests
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.guest_security import (
    compute_hermes_signature,
    enforce_guest_request_rate_limit,
    format_wib_timestamp,
    generate_short_code,
    get_client_ip,
    hash_token_sha256,
    normalize_short_code,
    parse_device_summary,
    verify_hermes_request,
)
from app.core.security import (
    ACCESS_COOKIE_NAME,
    create_guest_access_token,
    ensure_utc,
)
from app.db.session import SessionLocal, get_db
from app.models import GuestAccessRequest, GuestRequestStatus, GuestSession, UserRole
from app.schemas.auth import (
    GuestAccessRequestPayload,
    GuestAccessRequestResponse,
    GuestAccessStatusResponse,
    GuestApprovalActionPayload,
    GuestApprovalActionResponse,
    GuestApprovalDetailResponse,
    GuestExchangeRequest,
    HermesDecisionPayload,
    HermesDecisionResponse,
    HermesPendingRequestItem,
    HermesPendingResponse,
    TokenResponse,
    UserRead,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _post_hermes_webhook_once(
    url: str,
    body_bytes: bytes,
    headers: dict[str, str],
    timeout_seconds: float = 6.0,
) -> int:
    """Mengirim satu kali HTTP POST ke Hermes Bridge (dipisah agar mudah di-mock di pengujian)."""
    resp = requests.post(
        url,
        data=body_bytes,
        headers=headers,
        timeout=timeout_seconds,
    )
    return resp.status_code


def notify_hermes_webhook(
    request_id: UUID,
    payload: dict[str, Any],
    retry_backoff_seconds: float = 2.0,
) -> bool:
    """Background task pengiriman webhook HMAC-SHA256 ke CT 101 dengan retry 2x (backoff 2 detik)."""
    webhook_url = (settings.HERMES_WEBHOOK_URL or "").strip()
    if not webhook_url:
        return False

    body_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    max_attempts = 3  # 1 awal + 2 retry

    for attempt in range(1, max_attempts + 1):
        ts_str = str(int(datetime.now(timezone.utc).timestamp()))
        signature = compute_hermes_signature(
            settings.HERMES_SHARED_SECRET, ts_str, body_bytes
        )
        headers = {
            "Content-Type": "application/json",
            "X-Hermes-Timestamp": ts_str,
            "X-Hermes-Signature": signature,
        }
        try:
            status_code = _post_hermes_webhook_once(
                webhook_url,
                body_bytes,
                headers,
                timeout_seconds=6.0,
            )
            if 200 <= status_code < 300:
                db = SessionLocal()
                try:
                    req = (
                        db.query(GuestAccessRequest)
                        .filter(GuestAccessRequest.id == request_id)
                        .first()
                    )
                    if req is not None:
                        req.hermes_notified_at = datetime.now(timezone.utc)
                        db.commit()
                finally:
                    db.close()
                return True
            logger.warning(
                "Webhook Hermes (%s) mengembalikan status %s pada percobaan %s/%s.",
                webhook_url,
                status_code,
                attempt,
                max_attempts,
            )
        except Exception as exc:
            logger.warning(
                "Gagal mengirim webhook Hermes (%s) pada percobaan %s/%s: %s",
                webhook_url,
                attempt,
                max_attempts,
                exc,
            )

        if attempt < max_attempts and retry_backoff_seconds > 0:
            time.sleep(retry_backoff_seconds)

    return False


def _expire_stale_pending_requests(db: Session, now: datetime) -> bool:
    """Mengubah semua permintaan PENDING yang melewati batas 30 menit menjadi EXPIRED."""
    stale_rows = (
        db.query(GuestAccessRequest)
        .filter(
            GuestAccessRequest.status == GuestRequestStatus.PENDING.value,
            GuestAccessRequest.request_expires_at <= now,
        )
        .all()
    )
    if not stale_rows:
        return False

    for row in stale_rows:
        row.status = GuestRequestStatus.EXPIRED.value
    db.commit()
    return True


def _sync_single_expiry(db: Session, req: GuestAccessRequest, now: datetime) -> None:
    """Memastikan status PENDING pada satu record otomatis berubah ke EXPIRED bila lewat TTL."""
    if req.status == GuestRequestStatus.PENDING.value and now > ensure_utc(
        req.request_expires_at
    ):
        req.status = GuestRequestStatus.EXPIRED.value
        db.commit()
        db.refresh(req)


@router.post(
    "/request",
    response_model=GuestAccessRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def request_guest_access(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: Optional[GuestAccessRequestPayload] = None,
    db: Session = Depends(get_db),
) -> GuestAccessRequestResponse:
    """Mengajukan permintaan akses Guest sementara dan meneruskan notifikasi ke Hermes WhatsApp."""
    now = datetime.now(timezone.utc)
    ip_address = get_client_ip(request)
    enforce_guest_request_rate_limit(db, ip_address, now)

    user_agent = (request.headers.get("user-agent") or "Unknown").strip() or "Unknown"
    device_summary = parse_device_summary(user_agent)
    guest_name = (
        payload.guest_name.strip()[:100]
        if payload and payload.guest_name and payload.guest_name.strip()
        else None
    )

    confirm_token = secrets.token_urlsafe(32)
    wait_token = secrets.token_urlsafe(32)
    short_code = generate_short_code(db)
    request_expires_at = now + timedelta(minutes=settings.GUEST_REQUEST_TTL_MINUTES)

    guest_req = GuestAccessRequest(
        short_code=short_code,
        confirm_token_hash=hash_token_sha256(confirm_token),
        wait_token_hash=hash_token_sha256(wait_token),
        guest_name=guest_name,
        ip_address=ip_address,
        user_agent=user_agent,
        device_summary=device_summary,
        status=GuestRequestStatus.PENDING.value,
        requested_at=now,
        request_expires_at=request_expires_at,
    )
    db.add(guest_req)
    db.commit()
    db.refresh(guest_req)

    requested_at_wib = format_wib_timestamp(now)
    public_url = settings.APP_PUBLIC_URL.rstrip("/")
    approval_url = f"{public_url}/guest-approval/{confirm_token}"
    standard_message = (
        f"Terdapat permintaan masuk baru dari device {device_summary} "
        f"dengan ip {ip_address} pada {requested_at_wib}. "
        "Mohon konfirmasi apakah diizinkan masuk atau tidak? Yes No"
    )

    webhook_payload = {
        "event": "GIS_GUEST_ACCESS_REQUEST",
        "request_id": str(guest_req.id),
        "short_code": short_code,
        "guest_name": guest_name,
        "message": standard_message,
        "target_url": f"{public_url}/",
        "approval_url": approval_url,
        "device_summary": device_summary,
        "ip_address": ip_address,
        "requested_at_wib": requested_at_wib,
        "expires_in_minutes": settings.GUEST_REQUEST_TTL_MINUTES,
    }
    background_tasks.add_task(notify_hermes_webhook, guest_req.id, webhook_payload)

    return GuestAccessRequestResponse(
        short_code=short_code,
        wait_token=wait_token,
        status=GuestRequestStatus.PENDING,
        request_expires_at=request_expires_at,
        requested_at_wib=requested_at_wib,
        device_summary=device_summary,
        ip_address=ip_address,
    )


@router.get("/status", response_model=GuestAccessStatusResponse)
def get_guest_request_status(
    wait_token: str = Query(..., min_length=16, max_length=255),
    db: Session = Depends(get_db),
) -> GuestAccessStatusResponse:
    """Polling status permintaan akses Guest dari Waiting Room."""
    token_hash = hash_token_sha256(wait_token.strip())
    guest_req = (
        db.query(GuestAccessRequest)
        .filter(GuestAccessRequest.wait_token_hash == token_hash)
        .first()
    )
    if guest_req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permintaan akses Guest tidak ditemukan.",
        )

    now = datetime.now(timezone.utc)
    _sync_single_expiry(db, guest_req, now)

    return GuestAccessStatusResponse(
        status=GuestRequestStatus(guest_req.status),
        short_code=guest_req.short_code,
        request_expires_at=ensure_utc(guest_req.request_expires_at),
        decided_at=ensure_utc(guest_req.decided_at) if guest_req.decided_at else None,
    )


@router.post("/exchange", response_model=TokenResponse)
def exchange_guest_wait_token(
    payload: GuestExchangeRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Menukarkan wait_token berstatus APPROVED (single-use) menjadi JWT & sesi Guest 3 jam."""
    token_hash = hash_token_sha256(payload.wait_token.strip())
    guest_req = (
        db.query(GuestAccessRequest)
        .options(joinedload(GuestAccessRequest.session))
        .filter(GuestAccessRequest.wait_token_hash == token_hash)
        .first()
    )
    if guest_req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token tunggu Guest tidak ditemukan.",
        )

    now = datetime.now(timezone.utc)
    _sync_single_expiry(db, guest_req, now)

    if guest_req.session is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Token tunggu Guest ini sudah pernah ditukar menjadi sesi aktif (single-use).",
        )

    if guest_req.status == GuestRequestStatus.EXPIRED.value:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Permintaan akses Guest telah kedaluwarsa (melebihi 30 menit).",
        )
    if guest_req.status == GuestRequestStatus.REJECTED.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permintaan akses Anda ditolak oleh Administrator.",
        )
    if guest_req.status == GuestRequestStatus.REVOKED.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sesi akses Guest telah dicabut oleh Administrator.",
        )
    if guest_req.status != GuestRequestStatus.APPROVED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Permintaan akses Guest masih menunggu persetujuan Administrator.",
        )

    session_expires_at = now + timedelta(hours=settings.GUEST_SESSION_TTL_HOURS)
    guest_session = GuestSession(
        request_id=guest_req.id,
        ip_address=guest_req.ip_address,
        device_summary=guest_req.device_summary,
        started_at=now,
        expires_at=session_expires_at,
    )
    db.add(guest_session)
    db.commit()
    db.refresh(guest_session)

    access_token, expires_in = create_guest_access_token(
        guest_session,
        short_code=guest_req.short_code,
        guest_name=guest_req.guest_name,
    )
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=access_token,
        httponly=True,
        samesite="lax",
        max_age=expires_in,
        path="/",
    )

    display_name = f"Guest ({guest_req.short_code})" + (
        f" · {guest_req.guest_name}" if guest_req.guest_name else ""
    )
    guest_user = UserRead(
        id=guest_session.id,
        email=f"{guest_req.short_code.lower()}@guest.ldkh.local",
        full_name=display_name[:255],
        role=UserRole.GUEST,
        is_active=True,
        created_at=ensure_utc(guest_session.started_at),
        short_code=guest_req.short_code,
        session_expires_at=ensure_utc(guest_session.expires_at),
        ip_address=guest_session.ip_address,
        device_summary=guest_session.device_summary,
    )

    return TokenResponse(
        access_token=access_token,
        expires_in_seconds=expires_in,
        user=guest_user,
    )


@router.get("/hermes/pending", response_model=HermesPendingResponse)
def get_hermes_pending_requests(
    include_notified: bool = Query(
        default=False,
        description="Jika True, kembalikan seluruh permintaan PENDING aktif termasuk yang sudah pernah dinotifikasi.",
    ),
    _raw_body: bytes = Depends(verify_hermes_request),
    db: Session = Depends(get_db),
) -> HermesPendingResponse:
    """Endpoint privat ZeroTier untuk fallback recovery CT 101 mengambil request PENDING aktif."""
    now = datetime.now(timezone.utc)
    _expire_stale_pending_requests(db, now)

    query = db.query(GuestAccessRequest).filter(
        GuestAccessRequest.status == GuestRequestStatus.PENDING.value,
        GuestAccessRequest.request_expires_at > now,
    )
    if not include_notified:
        query = query.filter(GuestAccessRequest.hermes_notified_at.is_(None))

    rows = query.order_by(GuestAccessRequest.requested_at.asc()).all()
    public_url = settings.APP_PUBLIC_URL.rstrip("/")

    items: List[HermesPendingRequestItem] = []
    dirty = False
    for row in rows:
        if row.hermes_notified_at is None:
            row.hermes_notified_at = now
            dirty = True

        requested_at_utc = ensure_utc(row.requested_at)
        expires_at_utc = ensure_utc(row.request_expires_at)
        requested_at_wib = format_wib_timestamp(requested_at_utc)
        remaining_seconds = max(0, int((expires_at_utc - now).total_seconds()))
        msg = (
            f"Terdapat permintaan masuk baru dari device {row.device_summary} "
            f"dengan ip {row.ip_address} pada {requested_at_wib}. "
            "Mohon konfirmasi apakah diizinkan masuk atau tidak? Yes No"
        )
        items.append(
            HermesPendingRequestItem(
                request_id=row.id,
                short_code=row.short_code,
                guest_name=row.guest_name,
                device_summary=row.device_summary,
                ip_address=row.ip_address,
                requested_at=requested_at_utc,
                requested_at_wib=requested_at_wib,
                request_expires_at=expires_at_utc,
                remaining_seconds=remaining_seconds,
                target_url=f"{public_url}/",
                message=msg,
            )
        )

    if dirty:
        db.commit()

    return HermesPendingResponse(count=len(items), items=items)


@router.post("/hermes/decision", response_model=HermesDecisionResponse)
def process_hermes_decision(
    payload: HermesDecisionPayload,
    _raw_body: bytes = Depends(verify_hermes_request),
    db: Session = Depends(get_db),
) -> HermesDecisionResponse:
    """Endpoint privat ZeroTier untuk menerima keputusan APPROVE / REJECT / REVOKE dari Hermes CT 101."""
    now = datetime.now(timezone.utc)
    _expire_stale_pending_requests(db, now)

    short_code = normalize_short_code(payload.short_code)
    action = payload.action

    if action in ("APPROVE", "REJECT"):
        target_req: Optional[GuestAccessRequest] = None
        if short_code:
            target_req = (
                db.query(GuestAccessRequest)
                .filter(GuestAccessRequest.short_code == short_code)
                .first()
            )
            if target_req is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Permintaan akses dengan kode #{short_code} tidak ditemukan.",
                )
            if target_req.status == GuestRequestStatus.EXPIRED.value or now > ensure_utc(
                target_req.request_expires_at
            ):
                if target_req.status == GuestRequestStatus.PENDING.value:
                    target_req.status = GuestRequestStatus.EXPIRED.value
                    db.commit()
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail=(
                        f"Permintaan #{target_req.short_code} sudah kedaluwarsa "
                        "(melebihi batas waktu 30 menit)."
                    ),
                )
            if target_req.status != GuestRequestStatus.PENDING.value:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Permintaan #{target_req.short_code} sudah diproses sebelumnya "
                        f"dengan status {target_req.status}."
                    ),
                )
        else:
            active_pending = (
                db.query(GuestAccessRequest)
                .filter(
                    GuestAccessRequest.status == GuestRequestStatus.PENDING.value,
                    GuestAccessRequest.request_expires_at > now,
                )
                .order_by(GuestAccessRequest.requested_at.desc())
                .all()
            )
            if not active_pending:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        "Tidak ada permintaan akses guest yang aktif atau permintaan "
                        "sudah kedaluwarsa (melebihi 30 menit)."
                    ),
                )
            if len(active_pending) > 1:
                candidates = [
                    {
                        "short_code": item.short_code,
                        "guest_name": item.guest_name,
                        "device_summary": item.device_summary,
                        "ip_address": item.ip_address,
                        "requested_at_wib": format_wib_timestamp(
                            ensure_utc(item.requested_at)
                        ),
                    }
                    for item in active_pending
                ]
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": (
                            f"Terdapat {len(active_pending)} permintaan akses guest yang aktif. "
                            "Mohon sebutkan kode permintaan (contoh: Yes #G-XXXX atau No #G-XXXX)."
                        ),
                        "pending_requests": candidates,
                    },
                )
            target_req = active_pending[0]

        new_status = (
            GuestRequestStatus.APPROVED.value
            if action == "APPROVE"
            else GuestRequestStatus.REJECTED.value
        )
        target_req.status = new_status
        target_req.decided_at = now
        target_req.decided_via = "WHATSAPP_HERMES"
        db.commit()
        db.refresh(target_req)

        msg = (
            f"Permintaan #{target_req.short_code} ({target_req.device_summary} · {target_req.ip_address}) "
            f"{'DISETUJUI (akses Read-Only 3 jam)' if action == 'APPROVE' else 'DITOLAK'}."
        )
        return HermesDecisionResponse(
            short_code=target_req.short_code,
            status=GuestRequestStatus(target_req.status),
            action=action,
            decided_at=ensure_utc(target_req.decided_at or now),
            decided_via="WHATSAPP_HERMES",
            device_summary=target_req.device_summary,
            ip_address=target_req.ip_address,
            message=msg,
        )

    # action == "REVOKE"
    target_req = None
    if short_code:
        target_req = (
            db.query(GuestAccessRequest)
            .options(joinedload(GuestAccessRequest.session))
            .filter(GuestAccessRequest.short_code == short_code)
            .first()
        )
        if target_req is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Permintaan/sesi Guest #{short_code} tidak ditemukan.",
            )
    else:
        active_sessions = (
            db.query(GuestSession)
            .options(joinedload(GuestSession.request))
            .filter(
                GuestSession.revoked_at.is_(None),
                GuestSession.expires_at > now,
            )
            .order_by(GuestSession.started_at.desc())
            .all()
        )
        if not active_sessions:
            # Fallback: cek apakah ada request APPROVED yang belum ditukar ke sesi
            approved_unexchanged = (
                db.query(GuestAccessRequest)
                .options(joinedload(GuestAccessRequest.session))
                .filter(GuestAccessRequest.status == GuestRequestStatus.APPROVED.value)
                .order_by(GuestAccessRequest.decided_at.desc())
                .all()
            )
            if not approved_unexchanged:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Tidak ada sesi akses guest yang sedang aktif untuk dicabut.",
                )
            if len(approved_unexchanged) > 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "message": "Terdapat lebih dari satu sesi/persetujuan Guest aktif. Sebutkan kode #G-XXXX.",
                        "active_sessions": [
                            {
                                "short_code": r.short_code,
                                "device_summary": r.device_summary,
                                "ip_address": r.ip_address,
                            }
                            for r in approved_unexchanged
                        ],
                    },
                )
            target_req = approved_unexchanged[0]
        elif len(active_sessions) > 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "Terdapat lebih dari satu sesi Guest aktif. Sebutkan kode #G-XXXX untuk dicabut.",
                    "active_sessions": [
                        {
                            "short_code": s.request.short_code if s.request else "-",
                            "device_summary": s.device_summary,
                            "ip_address": s.ip_address,
                        }
                        for s in active_sessions
                    ],
                },
            )
        else:
            target_req = active_sessions[0].request

    if target_req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data permintaan akses Guest tidak ditemukan.",
        )

    if target_req.status in (
        GuestRequestStatus.REJECTED.value,
        GuestRequestStatus.EXPIRED.value,
        GuestRequestStatus.REVOKED.value,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Sesi #{target_req.short_code} sudah berstatus {target_req.status}.",
        )

    if target_req.session is not None and target_req.session.revoked_at is None:
        target_req.session.revoked_at = now
    target_req.status = GuestRequestStatus.REVOKED.value
    target_req.decided_at = now
    target_req.decided_via = "WHATSAPP_HERMES"
    db.commit()
    db.refresh(target_req)

    return HermesDecisionResponse(
        short_code=target_req.short_code,
        status=GuestRequestStatus.REVOKED,
        action="REVOKE",
        decided_at=now,
        decided_via="WHATSAPP_HERMES",
        device_summary=target_req.device_summary,
        ip_address=target_req.ip_address,
        message=f"Sesi Guest #{target_req.short_code} ({target_req.device_summary} · {target_req.ip_address}) berhasil dicabut.",
    )


@router.get("/approval/{confirm_token}", response_model=GuestApprovalDetailResponse)
def get_guest_approval_detail(
    confirm_token: str,
    db: Session = Depends(get_db),
) -> GuestApprovalDetailResponse:
    """Menampilkan detail permintaan Guest untuk halaman konfirmasi HP Owner (Unique Approval URL)."""
    token_hash = hash_token_sha256(confirm_token.strip())
    guest_req = (
        db.query(GuestAccessRequest)
        .filter(GuestAccessRequest.confirm_token_hash == token_hash)
        .first()
    )
    if guest_req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tautan persetujuan tidak ditemukan atau tidak valid.",
        )

    now = datetime.now(timezone.utc)
    _sync_single_expiry(db, guest_req, now)

    requested_at_utc = ensure_utc(guest_req.requested_at)
    expires_at_utc = ensure_utc(guest_req.request_expires_at)
    remaining_seconds = max(0, int((expires_at_utc - now).total_seconds()))

    return GuestApprovalDetailResponse(
        short_code=guest_req.short_code,
        guest_name=guest_req.guest_name,
        device_summary=guest_req.device_summary,
        ip_address=guest_req.ip_address,
        requested_at=requested_at_utc,
        requested_at_wib=format_wib_timestamp(requested_at_utc),
        request_expires_at=expires_at_utc,
        remaining_seconds=remaining_seconds,
        status=GuestRequestStatus(guest_req.status),
        decided_at=ensure_utc(guest_req.decided_at) if guest_req.decided_at else None,
        decided_via=guest_req.decided_via,
        is_actionable=guest_req.status == GuestRequestStatus.PENDING.value,
    )


@router.post("/approval/{confirm_token}", response_model=GuestApprovalActionResponse)
def submit_guest_approval_decision(
    confirm_token: str,
    payload: GuestApprovalActionPayload,
    db: Session = Depends(get_db),
) -> GuestApprovalActionResponse:
    """Memproses keputusan Owner dari Unique Approval URL (single-use)."""
    token_hash = hash_token_sha256(confirm_token.strip())
    guest_req = (
        db.query(GuestAccessRequest)
        .filter(GuestAccessRequest.confirm_token_hash == token_hash)
        .first()
    )
    if guest_req is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tautan persetujuan tidak ditemukan atau tidak valid.",
        )

    now = datetime.now(timezone.utc)
    _sync_single_expiry(db, guest_req, now)

    if guest_req.status == GuestRequestStatus.EXPIRED.value:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Tautan persetujuan sudah kedaluwarsa (melebihi batas waktu 30 menit).",
        )
    if guest_req.status != GuestRequestStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tautan persetujuan ini sudah pernah digunakan (Status: {guest_req.status}).",
        )

    new_status = (
        GuestRequestStatus.APPROVED.value
        if payload.action == "APPROVE"
        else GuestRequestStatus.REJECTED.value
    )
    guest_req.status = new_status
    guest_req.decided_at = now
    guest_req.decided_via = "APPROVAL_URL"
    db.commit()
    db.refresh(guest_req)

    return GuestApprovalActionResponse(
        short_code=guest_req.short_code,
        status=GuestRequestStatus(guest_req.status),
        decided_at=now,
        decided_via="APPROVAL_URL",
        message=(
            f"Permintaan akses #{guest_req.short_code} telah diizinkan (akses Read-Only 3 jam)."
            if payload.action == "APPROVE"
            else f"Permintaan akses #{guest_req.short_code} telah ditolak."
        ),
    )
