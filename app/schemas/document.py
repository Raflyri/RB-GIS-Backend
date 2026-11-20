"""Pydantic schemas untuk tabel `documents` (metadata surat, path Synology & signed PDF URL)."""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DocumentType, LegalStatus, VerificationStatus
from app.schemas.ocr import OcrRawDataCreate, OcrRawDataRead
from app.schemas.owner import OwnerRead


class DocumentBase(BaseModel):
    """Atribut dasar dokumen legal pertanahan LDKH."""

    parcel_id: Optional[UUID] = Field(
        default=None,
        description="Nullable untuk mendukung orphaned records sebelum lokasi fisik dipetakan.",
    )
    document_number: str = Field(..., min_length=1, max_length=128)
    document_type: DocumentType
    title: Optional[str] = Field(default=None, max_length=255)
    issue_date: Optional[date] = None
    issuing_office: Optional[str] = Field(default=None, max_length=255)
    legal_status: LegalStatus = LegalStatus.CLEAR
    original_area_value: Optional[Decimal] = Field(default=None, ge=0)
    original_area_unit: Optional[str] = Field(default="m2", max_length=32)
    synology_file_path: str = Field(..., min_length=1, max_length=512)
    nextcloud_file_id: Optional[str] = Field(default=None, max_length=128)
    file_checksum_sha256: Optional[str] = Field(default=None, max_length=64)
    verification_status: VerificationStatus = VerificationStatus.DRAFT_AI


class DocumentCreate(DocumentBase):
    """Payload pembuatan dokumen baru (opsional menyertakan hasil OCR awal)."""

    initial_ocr: Optional[OcrRawDataCreate] = None


class DocumentUpdate(BaseModel):
    """Payload pembaruan dokumen (termasuk penautan orphaned record ke parcel_id)."""

    parcel_id: Optional[UUID] = None
    document_number: Optional[str] = Field(default=None, min_length=1, max_length=128)
    document_type: Optional[DocumentType] = None
    title: Optional[str] = Field(default=None, max_length=255)
    issue_date: Optional[date] = None
    issuing_office: Optional[str] = Field(default=None, max_length=255)
    legal_status: Optional[LegalStatus] = None
    original_area_value: Optional[Decimal] = Field(default=None, ge=0)
    original_area_unit: Optional[str] = Field(default=None, max_length=32)
    synology_file_path: Optional[str] = Field(default=None, min_length=1, max_length=512)
    nextcloud_file_id: Optional[str] = Field(default=None, max_length=128)
    file_checksum_sha256: Optional[str] = Field(default=None, max_length=64)
    verification_status: Optional[VerificationStatus] = None
    verified_by: Optional[str] = Field(default=None, max_length=128)


class DocumentRead(DocumentBase):
    """Representasi respons ringkas dokumen legal."""

    id: UUID
    stated_area_m2: Optional[Decimal] = None
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    is_orphaned: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentDetailRead(DocumentRead):
    """Representasi detail dokumen lengkap dengan riwayat pemilik dan data OCR."""

    owners: List[OwnerRead] = Field(default_factory=list)
    ocr_raw_data: Optional[OcrRawDataRead] = None


class PdfSignedUrlResponse(BaseModel):
    """Respons signed URL sementara untuk streaming PDF inline di Split-pane Viewer."""

    document_id: UUID
    document_number: str
    stream_url: str
    expires_in_seconds: int
    content_disposition: str = "inline"
