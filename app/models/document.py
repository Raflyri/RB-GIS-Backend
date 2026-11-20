"""Model SQLAlchemy untuk tabel tabular `documents` (metadata surat legal & path Synology)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Date, DateTime, Enum as SAEnum, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.enums import DocumentType, LegalStatus, VerificationStatus

if TYPE_CHECKING:
    from app.models.ocr_raw_data import OcrRawData
    from app.models.owner import Owner
    from app.models.parcel import Parcel


class Document(Base):
    """Tabel data tabular untuk dokumen legal tanah (mendukung orphaned records jika parcel_id NULL)."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Nullable untuk mendukung orphaned records (surat sudah di-OCR, poligon belum ditemukan)
    parcel_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    document_number: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_type: Mapped[DocumentType] = mapped_column(
        SAEnum(DocumentType, name="document_type_enum"),
        nullable=False,
    )
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    issue_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    issuing_office: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    legal_status: Mapped[LegalStatus] = mapped_column(
        SAEnum(LegalStatus, name="legal_status_enum"),
        nullable=False,
        default=LegalStatus.CLEAR,
        index=True,
    )

    stated_area_m2: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    original_area_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 4), nullable=True
    )
    original_area_unit: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, default="m2"
    )

    # Referensi file fisik di Synology NAS / Nextcloud (dilarang menyimpan BLOB PDF di DB)
    synology_file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    nextcloud_file_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    file_checksum_sha256: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    verification_status: Mapped[VerificationStatus] = mapped_column(
        SAEnum(VerificationStatus, name="verification_status_enum"),
        nullable=False,
        default=VerificationStatus.DRAFT_AI,
        index=True,
    )
    verified_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    parcel: Mapped[Optional["Parcel"]] = relationship(
        "Parcel",
        back_populates="documents",
    )
    owners: Mapped[List["Owner"]] = relationship(
        "Owner",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    ocr_raw_data: Mapped[Optional["OcrRawData"]] = relationship(
        "OcrRawData",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def is_orphaned(self) -> bool:
        """Bernilai True apabila dokumen belum ditautkan ke poligon spasial."""
        return self.parcel_id is None
