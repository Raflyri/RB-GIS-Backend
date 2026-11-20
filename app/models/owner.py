"""Model SQLAlchemy untuk tabel `owners` (riwayat kepemilikan berantai)."""

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.enums import OwnerType

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.parcel import Parcel


class Owner(Base):
    """Tabel data tabular yang mencatat rantai riwayat kepemilikan tanah LDKH."""

    __tablename__ = "owners"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    parcel_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("parcels.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    owner_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    owner_type: Mapped[OwnerType] = mapped_column(
        SAEnum(OwnerType, name="owner_type_enum"),
        nullable=False,
        default=OwnerType.INDIVIDU,
    )
    identity_number: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current_owner: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    acquisition_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    acquisition_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    transfer_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
        back_populates="owners",
    )
    document: Mapped[Optional["Document"]] = relationship(
        "Document",
        back_populates="owners",
    )
