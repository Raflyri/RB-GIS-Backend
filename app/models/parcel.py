"""Model SQLAlchemy/GeoAlchemy2 untuk tabel spasial `parcels` (poligon persil tanah)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, Enum as SAEnum, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.enums import LegalStatus

if TYPE_CHECKING:
    from app.models.document import Document
    from app.models.owner import Owner


class Parcel(Base):
    """Tabel data spasial yang menyimpan poligon persil tanah LDKH (SRID 4326)."""

    __tablename__ = "parcels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    parcel_code: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    desa_kelurahan: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    kecamatan: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    kabupaten_kota: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, index=True
    )
    provinsi: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True, default="Jawa Barat"
    )
    alamat_lengkap: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    legal_status: Mapped[LegalStatus] = mapped_column(
        SAEnum(LegalStatus, name="legal_status_enum"),
        nullable=False,
        default=LegalStatus.CLEAR,
        index=True,
    )

    area_m2: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    gis_area_m2: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)
    original_area_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(14, 4), nullable=True
    )
    original_area_unit: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True, default="m2"
    )

    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=True),
        nullable=False,
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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

    documents: Mapped[List["Document"]] = relationship(
        "Document",
        back_populates="parcel",
    )
    owners: Mapped[List["Owner"]] = relationship(
        "Owner",
        back_populates="parcel",
        order_by="Owner.sequence_order",
    )
