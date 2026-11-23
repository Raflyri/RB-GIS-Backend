"""Utilitas keamanan Guest Access JIT & integrasi privat ZeroTier Hermes."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.guest import GuestAccessRequest

WIB_TZ = timezone(timedelta(hours=7))
_HARI_INDONESIA = (
    "Senin",
    "Selasa",
    "Rabu",
    "Kamis",
    "Jumat",
    "Sabtu",
    "Minggu",
)
# Alfabet uppercase tanpa karakter ambigu (O, 0, I, 1)
SHORT_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
GUEST_RATE_LIMIT_MAX = 3
GUEST_RATE_LIMIT_WINDOW_MINUTES = 15
HERMES_SIGNATURE_MAX_DRIFT_SECONDS = 300


def get_client_ip(request: Request) -> str:
    """Mengekstrak IP publik asli guest dengan prioritas CF-Connecting-IP -> X-Forwarded-For -> client.host."""
    cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf_ip:
        return cf_ip[:64]

    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        first_ip = xff.split(",")[0].strip()
        if first_ip:
            return first_ip[:64]

    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip[:64]

    if request.client and request.client.host:
        return request.client.host[:64]

    return "0.0.0.0"


def parse_device_summary(user_agent: str) -> str:
    """Mem-parse User-Agent menjadi ringkasan Browser + OS/Device yang mudah dibaca."""
    ua = (user_agent or "").strip()
    if not ua:
        return "Perangkat Tidak Dikenal"

    browser = "Browser"
    if match := re.search(r"(?:Edg|EdgA|EdgiOS)/(\d+)", ua):
        browser = f"Edge {match.group(1)}"
    elif match := re.search(r"OPR/(\d+)", ua):
        browser = f"Opera {match.group(1)}"
    elif match := re.search(r"SamsungBrowser/(\d+)", ua):
        browser = f"Samsung Browser {match.group(1)}"
    elif match := re.search(r"(?:Firefox|FxiOS)/(\d+)", ua):
        browser = f"Firefox {match.group(1)}"
    elif match := re.search(r"(?:Chrome|CriOS)/(\d+)", ua):
        browser = f"Chrome {match.group(1)}"
    elif match := re.search(r"Version/(\d+(?:\.\d+)?).*Safari/", ua):
        browser = f"Safari {match.group(1)}"
    elif match := re.search(r"WhatsApp/([\d.]+)", ua):
        browser = f"WhatsApp {match.group(1)}"
    elif match := re.search(r"curl/([\d.]+)", ua, re.IGNORECASE):
        browser = f"curl {match.group(1)}"

    os_label = "Unknown OS"
    if match := re.search(r"Android\s+([\d.]+)", ua):
        os_label = f"Android {match.group(1)}"
    elif "Android" in ua:
        os_label = "Android"
    elif "iPhone" in ua:
        if match := re.search(r"OS\s+([\d_]+)", ua):
            os_label = f"iPhone iOS {match.group(1).replace('_', '.')}"
        else:
            os_label = "iPhone iOS"
    elif "iPad" in ua:
        if match := re.search(r"OS\s+([\d_]+)", ua):
            os_label = f"iPadOS {match.group(1).replace('_', '.')}"
        else:
            os_label = "iPadOS"
    elif "Windows NT 10.0" in ua:
        os_label = "Windows 10/11"
    elif match := re.search(r"Windows NT\s+([\d.]+)", ua):
        os_label = f"Windows NT {match.group(1)}"
    elif match := re.search(r"Mac OS X\s+([\d_]+)", ua):
        os_label = f"macOS {match.group(1).replace('_', '.')}"
    elif "Linux" in ua:
        os_label = "Linux"

    summary = f"{browser} · {os_label}"
    return summary[:160]


def format_wib_timestamp(dt: Optional[datetime] = None) -> str:
    """Memformat waktu ke zona WIB (Asia/Jakarta): 'Hari, dd-mm-yyyy hh:mm:ss'."""
    target = dt or datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    wib_dt = target.astimezone(WIB_TZ)
    hari = _HARI_INDONESIA[wib_dt.weekday()]
    return f"{hari}, {wib_dt.strftime('%d-%m-%Y %H:%M:%S')}"


def hash_token_sha256(raw_token: str) -> str:
    """Menghasilkan digest SHA-256 hex 64-karakter dari token mentah."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_short_code(db: Session) -> str:
    """Menghasilkan kode unik format G-XXXX tanpa karakter ambigu O/0/I/1."""
    for _ in range(30):
        suffix = "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(4))
        candidate = f"G-{suffix}"
        exists = (
            db.query(GuestAccessRequest.id)
            .filter(GuestAccessRequest.short_code == candidate)
            .first()
        )
        if exists is None:
            return candidate
    raise RuntimeError("Gagal menghasilkan short_code Guest yang unik.")


def normalize_short_code(raw_code: Optional[str]) -> Optional[str]:
    """Menormalkan input short code seperti '#g-ab23' atau 'AB23' menjadi 'G-AB23'."""
    if raw_code is None:
        return None
    cleaned = raw_code.strip().upper().lstrip("#")
    if not cleaned:
        return None
    if len(cleaned) == 4 and not cleaned.startswith("G-"):
        return f"G-{cleaned}"
    return cleaned


def enforce_guest_request_rate_limit(db: Session, ip_address: str, now: datetime) -> None:
    """Membatasi maksimal 3 permintaan akses guest per IP dalam 15 menit."""
    window_start = now - timedelta(minutes=GUEST_RATE_LIMIT_WINDOW_MINUTES)
    recent_count = (
        db.query(func.count(GuestAccessRequest.id))
        .filter(
            GuestAccessRequest.ip_address == ip_address,
            GuestAccessRequest.requested_at >= window_start,
        )
        .scalar()
        or 0
    )
    if recent_count >= GUEST_RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Terlalu banyak permintaan akses Guest dari IP Anda "
                f"(maksimal {GUEST_RATE_LIMIT_MAX} permintaan dalam {GUEST_RATE_LIMIT_WINDOW_MINUTES} menit). "
                "Silakan tunggu beberapa saat sebelum mencoba lagi."
            ),
        )


def compute_hermes_signature(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Menghitung signature HMAC-SHA256 atas '{timestamp}.{raw_body}'."""
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


async def verify_hermes_request(request: Request) -> bytes:
    """Verifikasi ganda (IP Allowlist + HMAC-SHA256 Anti-Replay) untuk endpoint internal Hermes."""
    if (
        request.headers.get("x-forwarded-by-nginx", "").lower() == "true"
        or bool(request.headers.get("cf-connecting-ip"))
        or bool(request.headers.get("cf-ray"))
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akses endpoint internal Hermes melalui jaringan publik ditolak.",
        )

    client_ip = request.client.host if request.client and request.client.host else ""
    allowed_ips = set(settings.HERMES_ALLOWED_IPS)
    if client_ip not in allowed_ips:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Akses ditolak: IP {client_ip or 'unknown'} tidak masuk dalam HERMES_ALLOWED_IPS.",
        )

    timestamp_header = (request.headers.get("x-hermes-timestamp") or "").strip()
    signature_header = (request.headers.get("x-hermes-signature") or "").strip()
    if not timestamp_header or not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Header X-Hermes-Timestamp dan X-Hermes-Signature wajib disertakan.",
        )

    try:
        request_ts = int(timestamp_header)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Format X-Hermes-Timestamp harus berupa Unix epoch integer.",
        ) from exc

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if abs(now_ts - request_ts) > HERMES_SIGNATURE_MAX_DRIFT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Timestamp X-Hermes-Timestamp kedaluwarsa (selisih melebihi 300 detik).",
        )

    raw_body = await request.body()
    expected_sig = compute_hermes_signature(
        settings.HERMES_SHARED_SECRET, timestamp_header, raw_body
    )
    provided_sig = signature_header.lower().removeprefix("sha256=")
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signature HMAC-SHA256 Hermes tidak valid.",
        )

    return raw_body
