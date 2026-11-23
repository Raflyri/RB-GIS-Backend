"""Pengujian menyeluruh fitur Guest JIT Access via Hermes AI Agent & Strict Read-Only Mode."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@compiles(PGUUID, "sqlite")
def _compile_pg_uuid_sqlite(_element, _compiler, **_kw) -> str:
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _compile_pg_jsonb_sqlite(_element, _compiler, **_kw) -> str:
    return "TEXT"


from app.api.endpoints import guest_auth  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.guest_security import (  # noqa: E402
    compute_hermes_signature,
    format_wib_timestamp,
    parse_device_summary,
)
from app.core.security import ACCESS_COOKIE_NAME, hash_password  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Document,
    DocumentType,
    GuestAccessRequest,
    GuestRequestStatus,
    GuestSession,
    LegalStatus,
    OcrRawData,
    Owner,
    User,
    UserRole,
    VerificationStatus,
)


@pytest.fixture()
def test_env(monkeypatch):
    """Menyiapkan DB SQLite in-memory terisolasi dan TestClient FastAPI."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE parcels (
                    id CHAR(36) PRIMARY KEY,
                    parcel_code VARCHAR(64) NOT NULL UNIQUE
                )
                """
            )
        )
    for model in (
        User,
        GuestAccessRequest,
        GuestSession,
        Document,
        Owner,
        OcrRawData,
    ):
        model.__table__.create(bind=engine, checkfirst=True)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(guest_auth, "SessionLocal", TestingSessionLocal)

    sent_webhooks: list[dict[str, Any]] = []

    def fake_post_once(url: str, body_bytes: bytes, headers: dict[str, str], timeout_seconds: float = 6.0) -> int:
        sent_webhooks.append(
            {
                "url": url,
                "payload": json.loads(body_bytes.decode("utf-8")),
                "headers": headers,
                "raw_body": body_bytes,
            }
        )
        return 200

    monkeypatch.setattr(guest_auth, "_post_hermes_webhook_once", fake_post_once)
    monkeypatch.setattr(settings, "HERMES_SHARED_SECRET", "a" * 64)
    monkeypatch.setattr(settings, "HERMES_ALLOWED_IPS", ["testclient", "10.10.10.167"])
    monkeypatch.setattr(settings, "ENFORCE_REVERSE_PROXY", False)
    monkeypatch.setattr(settings, "GUEST_REQUEST_TTL_MINUTES", 30)
    monkeypatch.setattr(settings, "GUEST_SESSION_TTL_HOURS", 3)

    # Seed Admin user + sample document
    db = TestingSessionLocal()
    doc_id = uuid4()
    try:
        admin = User(
            id=uuid4(),
            email="admin@rbeverything.com",
            full_name="Admin LDKH",
            password_hash=hash_password("Secret123!"),
            role=UserRole.ADMIN,
            is_active=True,
        )
        doc = Document(
            id=doc_id,
            parcel_id=None,
            document_number="SHM-TEST-001",
            document_type=DocumentType.SHM,
            title="Dokumen Uji Read-Only Guest",
            legal_status=LegalStatus.CLEAR,
            synology_file_path="Sukaharja/SHM/SHM-00412-Sukaharja.pdf",
            verification_status=VerificationStatus.VERIFIED,
        )
        db.add(admin)
        db.add(doc)
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    yield {
        "client": client,
        "SessionLocal": TestingSessionLocal,
        "sent_webhooks": sent_webhooks,
        "sample_doc_id": str(doc_id),
    }

    app.dependency_overrides.clear()
    engine.dispose()


def _signed_hermes_headers(body_bytes: bytes, ts: int | None = None, secret: str | None = None) -> dict[str, str]:
    ts_str = str(ts if ts is not None else int(datetime.now(timezone.utc).timestamp()))
    sig = compute_hermes_signature(secret or settings.HERMES_SHARED_SECRET, ts_str, body_bytes)
    return {
        "Content-Type": "application/json",
        "X-Hermes-Timestamp": ts_str,
        "X-Hermes-Signature": sig,
    }


def test_ip_device_wib_and_rate_limit(test_env):
    client: TestClient = test_env["client"]
    sent_webhooks: list[dict[str, Any]] = test_env["sent_webhooks"]

    assert "Chrome 131" in parse_device_summary(
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/131.0.0.0 Mobile Safari/537.36"
    )
    wib_sample = format_wib_timestamp(datetime(2026, 11, 23, 8, 5, 9, tzinfo=timezone.utc))
    assert wib_sample == "Senin, 23-11-2026 15:05:09"

    headers = {
        "CF-Connecting-IP": "203.0.113.55",
        "X-Forwarded-For": "198.51.100.10, 172.16.10.133",
        "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/131.0.0.0 Mobile Safari/537.36",
    }

    for idx in range(3):
        resp = client.post(
            "/api/v1/auth/guest/request",
            json={"guest_name": f"Tamu {idx + 1}"},
            headers=headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["short_code"].startswith("G-")
        assert len(data["short_code"]) == 6
        assert data["ip_address"] == "203.0.113.55"
        assert "Chrome 131 · Android 14" == data["device_summary"]
        assert data["status"] == "PENDING"

    # Permintaan ke-4 dalam 15 menit harus ditolak dengan 429 Bahasa Indonesia
    resp4 = client.post(
        "/api/v1/auth/guest/request",
        json={"guest_name": "Tamu Spam"},
        headers=headers,
    )
    assert resp4.status_code == 429
    assert "maksimal 3 permintaan" in resp4.json()["detail"].lower()

    # Pastikan background webhook ke Hermes sudah terkirim dengan format pesan standar
    assert len(sent_webhooks) == 3
    webhook_payload = sent_webhooks[0]["payload"]
    assert "Terdapat permintaan masuk baru dari device Chrome 131 · Android 14 dengan ip 203.0.113.55 pada " in webhook_payload["message"]
    assert "Mohon konfirmasi apakah diizinkan masuk atau tidak? Yes No" in webhook_payload["message"]
    assert webhook_payload["approval_url"].startswith("https://gis.rbeverything.com/guest-approval/")
    assert webhook_payload["target_url"] == "https://gis.rbeverything.com/"


def test_hermes_double_layer_security(test_env, monkeypatch):
    client: TestClient = test_env["client"]
    body = json.dumps({"action": "APPROVE"}).encode("utf-8")

    # 1. IP tidak masuk allowlist -> 403 Forbidden
    monkeypatch.setattr(settings, "HERMES_ALLOWED_IPS", ["10.10.10.167"])
    resp_ip = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=body,
        headers=_signed_hermes_headers(body),
    )
    assert resp_ip.status_code == 403

    # Kembalikan allowlist untuk TestClient
    monkeypatch.setattr(settings, "HERMES_ALLOWED_IPS", ["testclient", "10.10.10.167"])

    # 2. Ada header proxy publik (Nginx / Cloudflare) -> 403 Forbidden
    headers_pub = _signed_hermes_headers(body)
    headers_pub["X-Forwarded-By-Nginx"] = "true"
    resp_pub = client.post("/api/v1/auth/guest/hermes/decision", content=body, headers=headers_pub)
    assert resp_pub.status_code == 403

    # 3. Signature HMAC salah -> 401 Unauthorized
    resp_bad_sig = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=body,
        headers=_signed_hermes_headers(body, secret="wrong-secret"),
    )
    assert resp_bad_sig.status_code == 401

    # 4. Timestamp kedaluwarsa (> 300 detik) -> 401 Unauthorized
    old_ts = int((datetime.now(timezone.utc) - timedelta(seconds=350)).timestamp())
    resp_old = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=body,
        headers=_signed_hermes_headers(body, ts=old_ts),
    )
    assert resp_old.status_code == 401

    # 5. Valid IP + HMAC saat tidak ada permintaan -> 404 Not Found dengan pesan jelas
    resp_empty = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=body,
        headers=_signed_hermes_headers(body),
    )
    assert resp_empty.status_code == 404
    assert "30 menit" in resp_empty.json()["detail"]

    # 6. Uji middleware ENFORCE_REVERSE_PROXY=True untuk rute /hermes/pending
    monkeypatch.setattr(settings, "ENFORCE_REVERSE_PROXY", True)
    req_resp = client.post(
        "/api/v1/auth/guest/request",
        json={"guest_name": "Recovery Test"},
        headers={"CF-Connecting-IP": "198.51.100.88", "X-Forwarded-By-Nginx": "true"},
    )
    assert req_resp.status_code == 201
    # Reset hermes_notified_at agar bisa ditarik via /hermes/pending
    db = test_env["SessionLocal"]()
    try:
        row = db.query(GuestAccessRequest).filter(GuestAccessRequest.short_code == req_resp.json()["short_code"]).first()
        assert row is not None
        row.hermes_notified_at = None
        db.commit()
    finally:
        db.close()

    pending_resp = client.get(
        "/api/v1/auth/guest/hermes/pending",
        headers=_signed_hermes_headers(b""),
    )
    assert pending_resp.status_code == 200
    assert pending_resp.json()["count"] == 1
    assert pending_resp.json()["items"][0]["short_code"] == req_resp.json()["short_code"]

    # Panggilan kedua tanpa include_notified harus 0 karena sudah ditandai notified
    pending_resp2 = client.get(
        "/api/v1/auth/guest/hermes/pending",
        headers=_signed_hermes_headers(b""),
    )
    assert pending_resp2.status_code == 200
    assert pending_resp2.json()["count"] == 0


def test_hermes_approval_exchange_and_30m_3h_expiry_and_revoke(test_env):
    client: TestClient = test_env["client"]
    SessionLocal = test_env["SessionLocal"]

    # 1. Buat request pertama
    req1 = client.post(
        "/api/v1/auth/guest/request",
        json={"guest_name": "Auditor Tamu 1"},
        headers={"CF-Connecting-IP": "198.51.100.21"},
    )
    assert req1.status_code == 201
    data1 = req1.json()
    wait1 = data1["wait_token"]
    code1 = data1["short_code"]

    # 2. Buat request kedua -> sekarang ada 2 PENDING aktif
    req2 = client.post(
        "/api/v1/auth/guest/request",
        json={"guest_name": "Auditor Tamu 2"},
        headers={"CF-Connecting-IP": "198.51.100.22"},
    )
    assert req2.status_code == 201
    data2 = req2.json()
    code2 = data2["short_code"]

    # 3. Approve tanpa short_code saat >1 pending -> 409 Conflict berisi kandidat
    ambiguous_body = json.dumps({"action": "APPROVE"}).encode("utf-8")
    resp_ambig = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=ambiguous_body,
        headers=_signed_hermes_headers(ambiguous_body),
    )
    assert resp_ambig.status_code == 409
    pending_codes = {item["short_code"] for item in resp_ambig.json()["detail"]["pending_requests"]}
    assert {code1, code2} == pending_codes

    # 4. Approve spesifik code1
    approve_body = json.dumps({"action": "APPROVE", "short_code": code1}).encode("utf-8")
    resp_approve = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=approve_body,
        headers=_signed_hermes_headers(approve_body),
    )
    assert resp_approve.status_code == 200
    assert resp_approve.json()["status"] == "APPROVED"

    # 5. Polling status & exchange wait_token (single-use)
    st1 = client.get(f"/api/v1/auth/guest/status?wait_token={wait1}")
    assert st1.status_code == 200
    assert st1.json()["status"] == "APPROVED"

    exch1 = client.post("/api/v1/auth/guest/exchange", json={"wait_token": wait1})
    assert exch1.status_code == 200
    guest_jwt = exch1.json()["access_token"]
    assert exch1.json()["user"]["role"] == "GUEST"
    assert exch1.json()["user"]["short_code"] == code1
    assert ACCESS_COOKIE_NAME in exch1.cookies

    # Single-use: tukar ulang harus ditolak 409
    exch1_again = client.post("/api/v1/auth/guest/exchange", json={"wait_token": wait1})
    assert exch1_again.status_code == 409

    # 6. Simulasikan request ke-2 melewati 30 menit -> auto-EXPIRED dan approval ditolak
    db = SessionLocal()
    try:
        row2 = db.query(GuestAccessRequest).filter(GuestAccessRequest.short_code == code2).first()
        assert row2 is not None
        row2.request_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    st2 = client.get(f"/api/v1/auth/guest/status?wait_token={data2['wait_token']}")
    assert st2.status_code == 200
    assert st2.json()["status"] == "EXPIRED"

    approve_expired = json.dumps({"action": "APPROVE", "short_code": code2}).encode("utf-8")
    resp_exp = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=approve_expired,
        headers=_signed_hermes_headers(approve_expired),
    )
    assert resp_exp.status_code == 410

    # 7. Verifikasi token Guest valid untuk /auth/me sebelum 3 jam
    me_ok = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {guest_jwt}"})
    assert me_ok.status_code == 200
    assert me_ok.json()["role"] == "GUEST"

    # 8. Simulasikan sesi Guest melewati 3 jam di DB -> 401 GUEST_SESSION_EXPIRED
    db = SessionLocal()
    try:
        sess = db.query(GuestSession).first()
        assert sess is not None
        sess.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.commit()
    finally:
        db.close()

    me_expired = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {guest_jwt}"})
    assert me_expired.status_code == 401
    assert "GUEST_SESSION_EXPIRED" in me_expired.json()["detail"]
    assert me_expired.headers.get("X-Error-Code") == "GUEST_SESSION_EXPIRED"

    # 9. Kembalikan expires_at lalu uji REVOKE via Hermes -> langsung 401 GUEST_SESSION_EXPIRED
    db = SessionLocal()
    try:
        sess = db.query(GuestSession).first()
        assert sess is not None
        sess.expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        db.commit()
    finally:
        db.close()

    revoke_body = json.dumps({"action": "REVOKE", "short_code": code1}).encode("utf-8")
    resp_revoke = client.post(
        "/api/v1/auth/guest/hermes/decision",
        content=revoke_body,
        headers=_signed_hermes_headers(revoke_body),
    )
    assert resp_revoke.status_code == 200
    assert resp_revoke.json()["status"] == "REVOKED"

    me_revoked = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {guest_jwt}"})
    assert me_revoked.status_code == 401
    assert "GUEST_SESSION_EXPIRED" in me_revoked.json()["detail"]


def test_unique_approval_url_single_use_and_strict_readonly_pdf(test_env):
    client: TestClient = test_env["client"]
    sent_webhooks: list[dict[str, Any]] = test_env["sent_webhooks"]
    sample_doc_id: str = test_env["sample_doc_id"]

    # 1. Ajukan permintaan guest baru
    req = client.post(
        "/api/v1/auth/guest/request",
        json={"guest_name": "BPN Tamu"},
        headers={"CF-Connecting-IP": "198.51.100.77"},
    )
    assert req.status_code == 201
    wait_token = req.json()["wait_token"]
    approval_url = sent_webhooks[-1]["payload"]["approval_url"]
    confirm_token = approval_url.rsplit("/", 1)[-1]

    # 2. Owner membuka Unique Approval URL (GET) -> is_actionable True
    detail_resp = client.get(f"/api/v1/auth/guest/approval/{confirm_token}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["is_actionable"] is True
    assert detail_resp.json()["ip_address"] == "198.51.100.77"

    # 3. Owner menyetujui via Unique Approval URL (POST) -> single-use
    post_approve = client.post(
        f"/api/v1/auth/guest/approval/{confirm_token}",
        json={"action": "APPROVE"},
    )
    assert post_approve.status_code == 200
    assert post_approve.json()["status"] == "APPROVED"

    # Pemakaian kedua harus ditolak 409 Conflict
    post_again = client.post(
        f"/api/v1/auth/guest/approval/{confirm_token}",
        json={"action": "REJECT"},
    )
    assert post_again.status_code == 409

    # 4. Exchange token untuk mendapat JWT GUEST
    exch = client.post("/api/v1/auth/guest/exchange", json={"wait_token": wait_token})
    assert exch.status_code == 200
    guest_headers = {"Authorization": f"Bearer {exch.json()['access_token']}"}

    # 5. Guest DIIZINKAN membaca dokumen & streaming PDF dengan header terhardening
    doc_list = client.get("/api/v1/documents/", headers=guest_headers)
    assert doc_list.status_code == 200

    pdf_url_resp = client.get(f"/api/v1/documents/{sample_doc_id}/pdf-url", headers=guest_headers)
    assert pdf_url_resp.status_code == 200
    stream_url = pdf_url_resp.json()["stream_url"]

    stream_resp = client.get(stream_url)
    assert stream_resp.status_code == 200
    assert stream_resp.headers["content-type"].startswith("application/pdf")
    assert stream_resp.headers["content-disposition"].startswith("inline")
    assert "no-store" in stream_resp.headers["cache-control"]
    assert "no-cache" in stream_resp.headers["cache-control"]
    assert "must-revalidate" in stream_resp.headers["cache-control"]
    assert "private" in stream_resp.headers["cache-control"]
    assert stream_resp.headers.get("pragma") == "no-cache"
    assert stream_resp.headers.get("x-content-type-options") == "nosniff"

    # 6. Guest WAJIB DITOLAK (403 Forbidden) pada SELURUH endpoint mutasi
    dummy_uuid = str(uuid4())
    mutation_checks = [
        client.post("/api/v1/parcels", json={}, headers=guest_headers),
        client.patch(f"/api/v1/parcels/{dummy_uuid}", json={}, headers=guest_headers),
        client.put(f"/api/v1/parcels/{dummy_uuid}", json={}, headers=guest_headers),
        client.delete(f"/api/v1/parcels/{dummy_uuid}", headers=guest_headers),
        client.post(
            "/api/v1/parcels/import",
            files={"file": ("test.geojson", b"{}", "application/json")},
            headers=guest_headers,
        ),
        client.post("/api/v1/documents/", json={}, headers=guest_headers),
        client.post(
            "/api/v1/documents/upload",
            files={"file": ("a.pdf", b"%PDF-1.4\n", "application/pdf")},
            headers=guest_headers,
        ),
        client.put(
            f"/api/v1/documents/{sample_doc_id}/file",
            files={"file": ("a.pdf", b"%PDF-1.4\n", "application/pdf")},
            headers=guest_headers,
        ),
        client.patch(f"/api/v1/documents/{sample_doc_id}", json={}, headers=guest_headers),
        client.delete(f"/api/v1/documents/{sample_doc_id}", headers=guest_headers),
        client.post("/api/v1/owners/", json={}, headers=guest_headers),
        client.patch(f"/api/v1/owners/{dummy_uuid}", json={}, headers=guest_headers),
        client.delete(f"/api/v1/owners/{dummy_uuid}", headers=guest_headers),
        client.post(f"/api/v1/ocr/document/{sample_doc_id}", json={}, headers=guest_headers),
        client.put(f"/api/v1/ocr/document/{sample_doc_id}/verify", json={}, headers=guest_headers),
    ]
    for resp in mutation_checks:
        assert resp.status_code == 403, f"Expected 403 for {resp.request.method} {resp.request.url}, got {resp.status_code}"


def test_hermes_bridge_package_webhook_and_owner_check(monkeypatch):
    bridge_path = REPO_ROOT / "integrations" / "hermes-guest-bridge" / "gis_hermes_bridge.py"
    monkeypatch.setenv("HERMES_SHARED_SECRET", "b" * 64)
    monkeypatch.setenv("ALLOWED_GIS_BACKEND_IP", "testclient,172.16.10.130")
    monkeypatch.setenv("ALLOWED_OWNER_WA_NUMBER", "6281111222233")
    monkeypatch.setenv("ENABLE_RECOVERY_LOOP", "false")

    spec = importlib.util.spec_from_file_location("gis_hermes_bridge_test", bridge_path)
    assert spec is not None and spec.loader is not None
    bridge_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_mod)

    delivered_messages: list[str] = []
    monkeypatch.setattr(
        bridge_mod,
        "send_whatsapp_to_owner",
        lambda msg, _payload=None: delivered_messages.append(msg) or True,
    )

    assert bridge_mod.verify_owner_sender("6281111222233@s.whatsapp.net") is True
    assert bridge_mod.verify_owner_sender("081111222233") is True
    assert bridge_mod.verify_owner_sender("6289999999999") is False

    bridge_client = TestClient(bridge_mod.app)
    webhook_body = json.dumps(
        {
            "short_code": "G-ABCD",
            "device_summary": "Chrome 131 · Android 14",
            "ip_address": "203.0.113.55",
            "requested_at_wib": "Senin, 23-11-2026 15:00:00",
            "approval_url": "https://gis.rbeverything.com/guest-approval/token123",
            "target_url": "https://gis.rbeverything.com/",
        }
    ).encode("utf-8")

    ts = str(int(datetime.now(timezone.utc).timestamp()))
    sig = bridge_mod.compute_hmac_signature("b" * 64, ts, webhook_body)

    bad_sig_resp = bridge_client.post(
        "/webhook/gis-guest",
        content=webhook_body,
        headers={
            "Content-Type": "application/json",
            "X-Hermes-Timestamp": ts,
            "X-Hermes-Signature": "deadbeef",
        },
    )
    assert bad_sig_resp.status_code == 401

    ok_resp = bridge_client.post(
        "/webhook/gis-guest",
        content=webhook_body,
        headers={
            "Content-Type": "application/json",
            "X-Hermes-Timestamp": ts,
            "X-Hermes-Signature": sig,
        },
    )
    assert ok_resp.status_code == 200
    assert ok_resp.json()["short_code"] == "G-ABCD"
    assert len(delivered_messages) == 1
    assert "#G-ABCD" in delivered_messages[0]
    assert "https://gis.rbeverything.com/guest-approval/token123" in delivered_messages[0]
