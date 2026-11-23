"""Endpoint REST API untuk validasi Human-in-the-Loop (HITL) hasil OCR Claude AI."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import require_admin, require_viewer
from app.db.session import get_db
from app.models import Document, LegalStatus, OcrRawData, User, VerificationStatus
from app.schemas import OcrHitlVerifyRequest, OcrRawDataCreate, OcrRawDataRead
from app.utils.area_normalizer import normalize_to_m2

router = APIRouter()


@router.get("/", response_model=List[OcrRawDataRead])
def list_ocr_records(
    verification_status: Optional[VerificationStatus] = Query(default=None),
    min_confidence: Optional[Decimal] = Query(default=None, ge=0, le=1),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> List[OcrRawData]:
    """Mengambil antrean data OCR untuk review Human-in-the-Loop."""
    query = db.query(OcrRawData)

    if verification_status is not None:
        query = query.filter(OcrRawData.verification_status == verification_status)
    if min_confidence is not None:
        query = query.filter(OcrRawData.confidence_score >= min_confidence)

    return (
        query.order_by(OcrRawData.processed_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/document/{document_id}", response_model=OcrRawDataRead)
def get_ocr_by_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> OcrRawData:
    """Mengambil hasil ekstraksi OCR mentah dan hasil revisi untuk suatu dokumen."""
    ocr = (
        db.query(OcrRawData)
        .filter(OcrRawData.document_id == document_id)
        .first()
    )
    if ocr is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Data OCR untuk dokumen {document_id} tidak ditemukan.",
        )
    return ocr


@router.post(
    "/document/{document_id}",
    response_model=OcrRawDataRead,
    status_code=status.HTTP_201_CREATED,
)
def upsert_initial_ocr(
    document_id: UUID,
    payload: OcrRawDataCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> OcrRawData:
    """Menyimpan atau memperbarui hasil ekstraksi awal OCR Claude AI (DRAFT_AI / NEEDS_REVIEW)."""
    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )

    ocr = (
        db.query(OcrRawData)
        .filter(OcrRawData.document_id == document_id)
        .first()
    )

    if ocr is None:
        ocr = OcrRawData(document_id=document_id, **payload.model_dump())
        db.add(ocr)
    else:
        for field, value in payload.model_dump().items():
            setattr(ocr, field, value)

    document.verification_status = payload.verification_status
    db.commit()
    db.refresh(ocr)
    return ocr


@router.put("/document/{document_id}/verify", response_model=OcrRawDataRead)
def verify_ocr_hitl(
    document_id: UUID,
    payload: OcrHitlVerifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> OcrRawData:
    """Endpoint Human-in-the-Loop untuk mengoreksi, memverifikasi, dan menyinkronkan data OCR ke tabel documents."""
    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )

    # Identitas reviewer wajib diambil dari sesi user terautentikasi (anti-spoofing)
    reviewer_identity = f"{current_user.full_name} ({current_user.email})"[:128]
    now = datetime.now(timezone.utc)
    ocr = (
        db.query(OcrRawData)
        .filter(OcrRawData.document_id == document_id)
        .first()
    )

    if ocr is None:
        ocr = OcrRawData(
            document_id=document_id,
            raw_text=payload.raw_text,
            extracted_json=payload.reviewed_json,
        )
        db.add(ocr)

    ocr.reviewed_json = payload.reviewed_json
    if payload.raw_text is not None:
        ocr.raw_text = payload.raw_text
    ocr.verification_status = payload.verification_status
    ocr.review_notes = payload.review_notes
    ocr.reviewed_by = reviewer_identity
    ocr.reviewed_at = now

    document.verification_status = payload.verification_status
    if payload.verification_status == VerificationStatus.VERIFIED:
        document.verified_by = reviewer_identity
        document.verified_at = now
    else:
        document.verified_by = None
        document.verified_at = None

    if payload.sync_to_document and payload.reviewed_json:
        reviewed = payload.reviewed_json

        doc_num = reviewed.get("document_number")
        if isinstance(doc_num, str) and doc_num.strip():
            document.document_number = doc_num.strip()

        title = reviewed.get("title")
        if isinstance(title, str) and title.strip():
            document.title = title.strip()

        issuing_office = reviewed.get("issuing_office")
        if isinstance(issuing_office, str) and issuing_office.strip():
            document.issuing_office = issuing_office.strip()

        raw_legal_status = reviewed.get("legal_status")
        if raw_legal_status is not None:
            try:
                document.legal_status = LegalStatus(str(raw_legal_status).strip().upper())
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Status hukum pada reviewed_json tidak valid: {raw_legal_status!r}",
                ) from exc

        raw_area_val = reviewed.get("area_value", reviewed.get("original_area_value"))
        raw_area_unit = reviewed.get(
            "area_unit",
            reviewed.get("original_area_unit", document.original_area_unit or "m2"),
        )
        if raw_area_val is not None:
            try:
                normalized_m2 = normalize_to_m2(raw_area_val, str(raw_area_unit))
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc

            document.original_area_value = Decimal(str(raw_area_val))
            document.original_area_unit = str(raw_area_unit)
            document.stated_area_m2 = normalized_m2

    db.commit()
    db.refresh(ocr)
    return ocr
