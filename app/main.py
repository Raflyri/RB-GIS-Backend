"""Entrypoint aplikasi FastAPI untuk Sistem Web GIS Digitalisasi Tanah LDKH."""

from pathlib import Path
from typing import Any, Dict

from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.router import api_router
from app.core.config import settings
from app.core.pdf_security import get_synology_base_dir
from app.core.security import require_viewer
from app.db.session import get_db
from app.models import Document, LegalStatus, OcrRawData, Parcel, User, VerificationStatus

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def reverse_proxy_guard_middleware(request: Request, call_next):
    """Menolak akses langsung ke port backend apabila ENFORCE_REVERSE_PROXY aktif."""
    if settings.ENFORCE_REVERSE_PROXY:
        client_ip = request.client.host if request.client else ""
        trusted_ips = set(settings.TRUSTED_PROXY_IPS)
        hermes_ips = set(settings.HERMES_ALLOWED_IPS)
        has_nginx_header = (
            request.headers.get("x-forwarded-by-nginx", "").lower() == "true"
        )
        host_header = (request.headers.get("host") or "").lower()
        is_direct_backend_port = ":8000" in host_header and client_ip not in trusted_ips
        is_hermes_internal_path = request.url.path.startswith(
            f"{settings.API_V1_STR}/auth/guest/hermes/"
        )

        if is_hermes_internal_path:
            # Jalur privat ZeroTier Hermes CT 101 -> langsung ke backend tanpa lewat Nginx publik
            if has_nginx_header or bool(request.headers.get("cf-connecting-ip")):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": "Akses endpoint internal Hermes melalui reverse proxy publik ditolak."
                    },
                )
            if client_ip not in hermes_ips:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": "Akses endpoint internal Hermes hanya diizinkan dari ZeroTier CT 101."
                    },
                )
            return await call_next(request)

        if request.url.path == f"{settings.API_V1_STR}/health":
            if not (client_ip in ("127.0.0.1", "::1", "testclient") or has_nginx_header):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "detail": "Akses langsung ke port backend ditolak. Gunakan reverse proxy Nginx / domain resmi."
                    },
                )
        elif is_direct_backend_port or not (client_ip in trusted_ips or has_nginx_header):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "detail": "Akses langsung ke port backend ditolak. Gunakan reverse proxy Nginx / domain resmi."
                },
            )

    return await call_next(request)


app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/", tags=["root"])
def root() -> Dict[str, str]:
    """Ringkasan informasi layanan API."""
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "docs": "/docs",
        "health": f"{settings.API_V1_STR}/health",
        "stats": f"{settings.API_V1_STR}/stats",
    }


@app.get(f"{settings.API_V1_STR}/health", tags=["health"])
def health_check() -> Dict[str, Any]:
    """Memeriksa kesiapan service Backend dan ketersediaan mount path Synology."""
    configured_mount = Path(settings.SYNOLOGY_MOUNT_PATH)
    resolved_base = get_synology_base_dir()
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "synology_mount": {
            "configured_path": str(configured_mount),
            "configured_accessible": configured_mount.exists() and configured_mount.is_dir(),
            "active_base_path": str(resolved_base),
            "active_accessible": resolved_base.exists() and resolved_base.is_dir(),
        },
    }


@app.get(f"{settings.API_V1_STR}/stats", tags=["stats"])
def get_dashboard_stats(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> Dict[str, Any]:
    """Ringkasan statistik persil spasial, dokumen orphaned, dan antrean review OCR."""
    total_parcels = db.query(func.count(Parcel.id)).scalar() or 0
    total_documents = db.query(func.count(Document.id)).scalar() or 0
    orphaned_documents = (
        db.query(func.count(Document.id)).filter(Document.parcel_id.is_(None)).scalar()
        or 0
    )

    parcel_status_rows = (
        db.query(Parcel.legal_status, func.count(Parcel.id))
        .group_by(Parcel.legal_status)
        .all()
    )
    parcels_by_status = {status.value: 0 for status in LegalStatus}
    for legal_status, count in parcel_status_rows:
        parcels_by_status[legal_status.value] = count

    ocr_status_rows = (
        db.query(OcrRawData.verification_status, func.count(OcrRawData.id))
        .group_by(OcrRawData.verification_status)
        .all()
    )
    ocr_by_status = {status.value: 0 for status in VerificationStatus}
    for ver_status, count in ocr_status_rows:
        ocr_by_status[ver_status.value] = count

    total_area_m2 = db.query(func.coalesce(func.sum(Parcel.area_m2), 0)).scalar() or 0

    return {
        "total_parcels": total_parcels,
        "total_documents": total_documents,
        "orphaned_documents": orphaned_documents,
        "total_area_m2": float(total_area_m2),
        "parcels_by_legal_status": parcels_by_status,
        "ocr_by_verification_status": ocr_by_status,
    }
