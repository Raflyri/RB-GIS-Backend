"""Pydantic schemas untuk tabel `ocr_raw_data` & validasi Human-in-the-Loop."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import VerificationStatus


class OcrRawDataBase(BaseModel):
    """Atribut dasar hasil ekstraksi OCR Claude AI."""

    ocr_engine: str = Field(default="claude-3-5-sonnet", max_length=64)
    raw_text: Optional[str] = None
    extracted_json: Dict[str, Any] = Field(default_factory=dict)
    confidence_score: Optional[Decimal] = Field(default=None, ge=0, le=1)


class OcrRawDataCreate(OcrRawDataBase):
    """Payload penyimpanan hasil awal OCR AI."""

    verification_status: VerificationStatus = VerificationStatus.DRAFT_AI
    review_notes: Optional[str] = None


class OcrHitlVerifyRequest(BaseModel):
    """Payload validasi Human-in-the-Loop untuk mengoreksi dan memverifikasi hasil OCR."""

    reviewed_json: Dict[str, Any]
    raw_text: Optional[str] = None
    verification_status: VerificationStatus = VerificationStatus.VERIFIED
    review_notes: Optional[str] = None
    reviewed_by: Optional[str] = Field(default=None, max_length=128)
    sync_to_document: bool = Field(
        default=True,
        description="Sinkronkan field terverifikasi (no. surat, luasan ternormalisasi ke m2, status hukum) ke tabel documents.",
    )


class OcrRawDataRead(OcrRawDataBase):
    """Representasi respons record OCR dan status verifikasi HITL."""

    id: UUID
    document_id: UUID
    reviewed_json: Optional[Dict[str, Any]] = None
    verification_status: VerificationStatus
    review_notes: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    processed_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
