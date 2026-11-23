"""Model SQLAlchemy untuk fitur Just-in-Time Guest Access & Sesi Guest."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.enums import GuestRequestStatus


class GuestAccessRequest(Base):
    """Permintaan akses sementara Guest yang diverifikasi via Hermes WhatsApp / Approval URL."""

    __tablename__ = "guest_access_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    short_code: Mapped[str] = mapped_column(
        String(12),
        unique=True,
        nullable=False,
        index=True,
    )
    confirm_token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    wait_token_hash: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )
    guest_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_agent: Mapped[str] = mapped_column(Text, nullable=False)
    device_summary: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=GuestRequestStatus.PENDING.value,
        index=True,
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
    request_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    decided_via: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    hermes_notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    session: Mapped[Optional["GuestSession"]] = relationship(
        "GuestSession",
        back_populates="request",
        uselist=False,
        cascade="all, delete-orphan",
    )


class GuestSession(Base):
    """Sesi akses aktif Guest berdurasi maksimal 3 jam setelah disetujui."""

    __tablename__ = "guest_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("guest_access_requests.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False)
    device_summary: Mapped[str] = mapped_column(String(160), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    request: Mapped[GuestAccessRequest] = relationship(
        "GuestAccessRequest",
        back_populates="session",
    )
