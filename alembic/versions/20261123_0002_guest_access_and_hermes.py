"""Guest access requests and sessions for Hermes JIT approval

Revision ID: 0002_guest_access_and_hermes
Revises: 0001_initial_ldkh_gis_schema
Create Date: 2026-11-23 15:15:00

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002_guest_access_and_hermes"
down_revision: Union[str, None] = "0001_initial_ldkh_gis_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # Pastikan enum user_role_enum dan nilai GUEST tersedia (top-level statement di luar DO block)
    if bind.dialect.name == "postgresql":
        has_enum = bind.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = 'user_role_enum'")
        ).scalar()
        if not has_enum:
            op.execute(
                "CREATE TYPE user_role_enum AS ENUM ('ADMIN', 'AUDITOR', 'VIEWER', 'GUEST')"
            )
        else:
            with op.get_context().autocommit_block():
                op.execute("ALTER TYPE user_role_enum ADD VALUE IF NOT EXISTS 'GUEST'")

    op.create_table(
        "guest_access_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("short_code", sa.String(length=12), nullable=False),
        sa.Column("confirm_token_hash", sa.String(length=64), nullable=False),
        sa.Column("wait_token_hash", sa.String(length=64), nullable=False),
        sa.Column("guest_name", sa.String(length=100), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=False),
        sa.Column("device_summary", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("request_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_via", sa.String(length=32), nullable=True),
        sa.Column("hermes_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("short_code", name="uq_guest_access_requests_short_code"),
        sa.UniqueConstraint(
            "confirm_token_hash", name="uq_guest_access_requests_confirm_token_hash"
        ),
        sa.UniqueConstraint(
            "wait_token_hash", name="uq_guest_access_requests_wait_token_hash"
        ),
    )
    op.create_index(
        "ix_guest_access_requests_short_code",
        "guest_access_requests",
        ["short_code"],
        unique=True,
    )
    op.create_index(
        "ix_guest_access_requests_confirm_token_hash",
        "guest_access_requests",
        ["confirm_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_guest_access_requests_wait_token_hash",
        "guest_access_requests",
        ["wait_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_guest_access_requests_ip_address",
        "guest_access_requests",
        ["ip_address"],
    )
    op.create_index(
        "ix_guest_access_requests_status",
        "guest_access_requests",
        ["status"],
    )
    op.create_index(
        "ix_guest_access_requests_requested_at",
        "guest_access_requests",
        ["requested_at"],
    )

    op.create_table(
        "guest_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guest_access_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("device_summary", sa.String(length=160), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("request_id", name="uq_guest_sessions_request_id"),
    )
    op.create_index(
        "ix_guest_sessions_request_id",
        "guest_sessions",
        ["request_id"],
        unique=True,
    )
    op.create_index(
        "ix_guest_sessions_expires_at",
        "guest_sessions",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_guest_sessions_expires_at", table_name="guest_sessions")
    op.drop_index("ix_guest_sessions_request_id", table_name="guest_sessions")
    op.drop_table("guest_sessions")

    op.drop_index("ix_guest_access_requests_requested_at", table_name="guest_access_requests")
    op.drop_index("ix_guest_access_requests_status", table_name="guest_access_requests")
    op.drop_index("ix_guest_access_requests_ip_address", table_name="guest_access_requests")
    op.drop_index(
        "ix_guest_access_requests_wait_token_hash",
        table_name="guest_access_requests",
    )
    op.drop_index(
        "ix_guest_access_requests_confirm_token_hash",
        table_name="guest_access_requests",
    )
    op.drop_index(
        "ix_guest_access_requests_short_code",
        table_name="guest_access_requests",
    )
    op.drop_table("guest_access_requests")
