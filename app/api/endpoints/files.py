"""Endpoint streaming PDF aman via Signed Token, mount point Synology, dan Nginx X-Accel-Redirect."""

from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.pdf_security import (
    generate_sample_pdf_bytes,
    resolve_synology_file_path,
    sanitize_relative_pdf_path,
    verify_pdf_stream_token,
)
from app.db.session import get_db
from app.models import Document

router = APIRouter()


@router.get("/stream/{document_id}")
def stream_document_pdf(
    document_id: UUID,
    request: Request,
    token: str = Query(..., description="Signed JWT token dari endpoint /documents/{id}/pdf-url"),
    delivery: Literal["auto", "direct", "accel"] = Query(
        default="auto",
        description="Mode pengiriman file: 'accel' (Nginx X-Accel-Redirect), 'direct' (FastAPI FileResponse), atau 'auto'.",
    ),
    db: Session = Depends(get_db),
) -> Response:
    """Men-stream dokumen PDF secara inline setelah memvalidasi signed token dan path Synology."""
    try:
        verify_pdf_stream_token(token, expected_document_id=document_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )

    try:
        clean_relative_path = sanitize_relative_pdf_path(document.synology_file_path)
        target_path = resolve_synology_file_path(clean_relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    filename = Path(clean_relative_path).name or f"{document.document_number}.pdf"
    hardened_headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }

    if (
        delivery == "accel"
        and target_path.exists()
        and target_path.is_file()
        and str(target_path).startswith(str(Path(settings.SYNOLOGY_MOUNT_PATH).resolve()))
    ):
        internal_prefix = settings.NGINX_INTERNAL_PDF_PREFIX.rstrip("/")
        return Response(
            content=b"",
            status_code=status.HTTP_200_OK,
            headers={
                "X-Accel-Redirect": f"{internal_prefix}/{clean_relative_path}",
                "Content-Type": "application/pdf",
                **hardened_headers,
            },
        )

    if target_path.exists() and target_path.is_file():
        return FileResponse(
            path=target_path,
            media_type="application/pdf",
            headers=hardened_headers,
        )

    # Fallback apabila file fisik scan PDF belum diunggah ke server/Synology
    sample_bytes = generate_sample_pdf_bytes(
        title=document.title or document.document_number,
        doc_number=document.document_number,
        synology_path=clean_relative_path,
    )
    return Response(
        content=sample_bytes,
        media_type="application/pdf",
        headers={
            **hardened_headers,
            "X-LDKH-Fallback-Sample": "true",
        },
    )
