"""Pydantic schemas untuk tabel `owners` (riwayat kepemilikan berantai)."""

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OwnerType


class OwnerBase(BaseModel):
    """Atribut dasar catatan kepemilikan tanah."""

    parcel_id: Optional[UUID] = None
    document_id: Optional[UUID] = None
    owner_name: str = Field(..., min_length=1, max_length=255)
    owner_type: OwnerType = OwnerType.INDIVIDU
    identity_number: Optional[str] = Field(default=None, max_length=64)
    sequence_order: int = Field(default=1, ge=1)
    is_current_owner: bool = True
    acquisition_type: Optional[str] = Field(default=None, max_length=64)
    acquisition_date: Optional[date] = None
    transfer_notes: Optional[str] = None


class OwnerCreate(OwnerBase):
    """Payload pembuatan riwayat pemilik baru."""

    mark_previous_non_current: bool = Field(
        default=True,
        description="Jika True dan is_current_owner=True, pemilik sebelumnya pada persil/dokumen ditandai non-current.",
    )


class OwnerUpdate(BaseModel):
    """Payload pembaruan sebagian catatan kepemilikan."""

    parcel_id: Optional[UUID] = None
    document_id: Optional[UUID] = None
    owner_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    owner_type: Optional[OwnerType] = None
    identity_number: Optional[str] = Field(default=None, max_length=64)
    sequence_order: Optional[int] = Field(default=None, ge=1)
    is_current_owner: Optional[bool] = None
    acquisition_type: Optional[str] = Field(default=None, max_length=64)
    acquisition_date: Optional[date] = None
    transfer_notes: Optional[str] = None


class OwnerRead(OwnerBase):
    """Representasi respons catatan kepemilikan."""

    id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
