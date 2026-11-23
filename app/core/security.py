"""Utilitas keamanan autentikasi, hashing password, JWT sesi, Cloudflare Access, & RBAC."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable, Optional
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.db.session import get_db
from app.models.enums import GuestRequestStatus, UserRole
from app.models.guest import GuestSession
from app.models.user import User

ALGORITHM = "HS256"
PBKDF2_ITERATIONS = 310_000
ACCESS_COOKIE_NAME = "ldkh_access_token"
GUEST_SESSION_EXPIRED_CODE = "GUEST_SESSION_EXPIRED"

bearer_scheme = HTTPBearer(auto_error=False)


class GuestSessionExpiredError(ValueError):
    """Error khusus ketika sesi Guest 3 jam berakhir atau dicabut oleh Owner."""


def ensure_utc(dt: datetime) -> datetime:
    """Memastikan objek datetime selalu timezone-aware UTC (aman untuk SQLite/PostgreSQL)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Menghasilkan hash password PBKDF2-HMAC-SHA256 dengan salt acak 16 byte."""
    salt = secrets.token_bytes(16)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived_key.hex()}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """Memverifikasi password plain-text terhadap hash PBKDF2-SHA256 tersimpan."""
    try:
        scheme, iter_str, salt_hex, expected_hex = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iter_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def create_access_token(user: User) -> tuple[str, int]:
    """Membuat JWT sesi aplikasi yang memuat ID user, email, dan role."""
    now = datetime.now(timezone.utc)
    expires_in = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role.value,
        "scope": "ldkh:auth",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    return token, expires_in


def create_guest_access_token(
    session: GuestSession,
    short_code: str,
    guest_name: Optional[str] = None,
) -> tuple[str, int]:
    """Membuat JWT sesi sementara Guest (berlaku sesuai expires_at di tabel guest_sessions)."""
    now = datetime.now(timezone.utc)
    expires_at = ensure_utc(session.expires_at)
    expires_in = max(1, int((expires_at - now).total_seconds()))
    payload: dict[str, Any] = {
        "sub": f"guest:{session.id}",
        "sid": str(session.id),
        "role": UserRole.GUEST.value,
        "short_code": short_code,
        "guest_name": guest_name,
        "scope": "ldkh:auth",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    """Memverifikasi JWT sesi aplikasi dan memastikan scope valid."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        try:
            unexpired_payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[ALGORITHM],
                options={"verify_exp": False},
            )
            if unexpired_payload.get("role") == UserRole.GUEST.value:
                raise GuestSessionExpiredError(
                    f"{GUEST_SESSION_EXPIRED_CODE}: Sesi akses Guest 3 jam telah berakhir. "
                    "Silakan ajukan permintaan masuk kembali."
                ) from exc
        except GuestSessionExpiredError:
            raise
        except jwt.InvalidTokenError:
            pass
        raise ValueError("Sesi login telah kedaluwarsa. Silakan login ulang.") from exc
    except jwt.InvalidTokenError as exc:
        raise ValueError("Token autentikasi tidak valid.") from exc

    if payload.get("scope") != "ldkh:auth":
        raise ValueError("Scope token autentikasi tidak valid.")
    return payload


@lru_cache(maxsize=4)
def _get_cf_jwk_client(certs_url: str) -> jwt.PyJWKClient:
    """Menginisialisasi dan meng-cache PyJWKClient untuk sertifikat publik Cloudflare Access."""
    return jwt.PyJWKClient(certs_url, cache_keys=True)


def verify_cloudflare_access_jwt(assertion: str) -> dict[str, Any]:
    """Memverifikasi header Cf-Access-Jwt-Assertion terhadap JWKS Cloudflare Zero Trust."""
    raw_domain = (settings.CF_ACCESS_TEAM_DOMAIN or "").strip()
    if not raw_domain:
        raise ValueError(
            "CF_ACCESS_TEAM_DOMAIN belum dikonfigurasi untuk verifikasi token Cloudflare Access."
        )

    team_domain = raw_domain.removeprefix("https://").removeprefix("http://").strip("/")
    certs_url = f"https://{team_domain}/cdn-cgi/access/certs"
    expected_issuer = f"https://{team_domain}"
    expected_aud = (settings.CF_ACCESS_AUD or "").strip() or None

    try:
        jwk_client = _get_cf_jwk_client(certs_url)
        signing_key = jwk_client.get_signing_key_from_jwt(assertion)
        decode_kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "issuer": expected_issuer,
        }
        if expected_aud:
            decode_kwargs["audience"] = expected_aud
        else:
            decode_kwargs["options"] = {"verify_aud": False}

        return jwt.decode(assertion, signing_key.key, **decode_kwargs)
    except jwt.PyJWTError as exc:
        raise ValueError(f"Token Cloudflare Access tidak valid: {exc}") from exc


def _resolve_guest_from_claims(db: Session, payload: dict[str, Any]) -> User:
    """Memvalidasi sesi Guest terhadap tabel guest_sessions (cek revoke & batas 3 jam)."""
    try:
        session_id = UUID(str(payload.get("sid")))
    except (ValueError, TypeError) as exc:
        raise GuestSessionExpiredError(
            f"{GUEST_SESSION_EXPIRED_CODE}: Identitas sesi Guest tidak valid."
        ) from exc

    guest_session = (
        db.query(GuestSession)
        .options(joinedload(GuestSession.request))
        .filter(GuestSession.id == session_id)
        .first()
    )
    if guest_session is None:
        raise GuestSessionExpiredError(
            f"{GUEST_SESSION_EXPIRED_CODE}: Sesi akses Guest tidak ditemukan atau telah berakhir."
        )

    now = datetime.now(timezone.utc)
    expires_at = ensure_utc(guest_session.expires_at)
    req = guest_session.request
    if (
        guest_session.revoked_at is not None
        or now >= expires_at
        or (req is not None and req.status == GuestRequestStatus.REVOKED.value)
    ):
        raise GuestSessionExpiredError(
            f"{GUEST_SESSION_EXPIRED_CODE}: Sesi akses Guest 3 jam telah berakhir atau dicabut. "
            "Silakan ajukan permintaan masuk kembali."
        )

    short_code = req.short_code if req else str(payload.get("short_code") or "GUEST")
    guest_name = req.guest_name if req else payload.get("guest_name")
    display_name = f"Guest ({short_code})" + (f" · {guest_name}" if guest_name else "")

    guest_user = User(
        id=guest_session.id,
        email=f"{short_code.lower()}@guest.ldkh.local",
        full_name=display_name[:255],
        password_hash="",
        role=UserRole.GUEST,
        is_active=True,
        created_at=ensure_utc(guest_session.started_at),
    )
    guest_user.short_code = short_code
    guest_user.session_expires_at = expires_at
    guest_user.ip_address = guest_session.ip_address
    guest_user.device_summary = guest_session.device_summary
    return guest_user


def _resolve_user_from_app_token(db: Session, token: str) -> User:
    payload = decode_access_token(token)
    if payload.get("role") == UserRole.GUEST.value:
        return _resolve_guest_from_claims(db, payload)

    try:
        user_id = UUID(str(payload.get("sub")))
    except (ValueError, TypeError) as exc:
        raise ValueError("Identitas user pada token tidak valid.") from exc

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise ValueError("Akun pengguna tidak ditemukan atau sudah dinonaktifkan.")
    return user


def _resolve_user_from_cloudflare_jwt(db: Session, assertion: str) -> User:
    claims = verify_cloudflare_access_jwt(assertion)
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise ValueError("Token Cloudflare Access tidak memuat klaim email.")

    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Email Cloudflare Access ({email}) belum terdaftar atau tidak aktif di Web GIS LDKH.",
        )
    return user


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Mengautentikasi user dari Bearer JWT, cookie sesi, atau Cf-Access-Jwt-Assertion."""
    app_token: Optional[str] = None
    if credentials and credentials.scheme.lower() == "bearer" and credentials.credentials:
        app_token = credentials.credentials.strip()
    elif request.cookies.get(ACCESS_COOKIE_NAME):
        app_token = request.cookies[ACCESS_COOKIE_NAME].strip()

    if app_token:
        try:
            return _resolve_user_from_app_token(db, app_token)
        except GuestSessionExpiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={
                    "WWW-Authenticate": "Bearer",
                    "X-Error-Code": GUEST_SESSION_EXPIRED_CODE,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    cf_assertion = (
        request.headers.get("cf-access-jwt-assertion")
        or request.cookies.get("CF_Authorization")
        or ""
    ).strip()
    if cf_assertion:
        try:
            return _resolve_user_from_cloudflare_jwt(db, cf_assertion)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Autentikasi diperlukan. Silakan login terlebih dahulu.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_roles(*allowed_roles: UserRole) -> Callable[[User], User]:
    """Dependency RBAC yang membatasi akses endpoint hanya untuk role tertentu."""

    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            allowed_label = ", ".join(r.value for r in allowed_roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Akses ditolak untuk peran {current_user.role.value}. "
                    f"Peran yang diizinkan: {allowed_label}."
                ),
            )
        return current_user

    return dependency


require_viewer = require_roles(
    UserRole.GUEST,
    UserRole.VIEWER,
    UserRole.AUDITOR,
    UserRole.ADMIN,
)
require_editor = require_roles(UserRole.ADMIN)
require_admin = require_roles(UserRole.ADMIN)
