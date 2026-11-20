"""Initial LDKH GIS schema (parcels, documents, owners, ocr_raw_data)

Revision ID: 0001_initial_ldkh_gis_schema
Revises:
Create Date: 2026-11-17 14:40:00

"""
from typing import Sequence, Union

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial_ldkh_gis_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


legal_status_enum = postgresql.ENUM(
    "CLEAR",
    "SENGKETA",
    "BEDA_FISIK",
    "DISITA_BI",
    "RAMPASAN",
    name="legal_status_enum",
    create_type=False,
)

document_type_enum = postgresql.ENUM(
    "SHM",
    "SHGB",
    "AJB",
    "GIRIK",
    "LETTER_C",
    "SPH",
    "PUTUSAN_PENGADILAN",
    "BERITA_ACARA_SITA",
    "LAINNYA",
    name="document_type_enum",
    create_type=False,
)

verification_status_enum = postgresql.ENUM(
    "DRAFT_AI",
    "NEEDS_REVIEW",
    "VERIFIED",
    name="verification_status_enum",
    create_type=False,
)

owner_type_enum = postgresql.ENUM(
    "INDIVIDU",
    "KORPORASI",
    "INSTANSI_PEMERINTAH",
    "BANK_INDONESIA",
    "AHLI_WARIS",
    name="owner_type_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    op.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')

    # Fungsi SQL normalisasi luasan ke Meter Persegi (m2)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION normalize_area_to_m2(val NUMERIC, unit TEXT)
        RETURNS NUMERIC AS $$
        DECLARE
            normalized_unit TEXT;
        BEGIN
            IF val IS NULL THEN
                RETURN NULL;
            END IF;

            normalized_unit := LOWER(TRIM(COALESCE(unit, 'm2')));

            RETURN CASE normalized_unit
                WHEN 'm2' THEN ROUND(val, 2)
                WHEN 'm²' THEN ROUND(val, 2)
                WHEN 'meter_persegi' THEN ROUND(val, 2)
                WHEN 'meter persegi' THEN ROUND(val, 2)
                WHEN 'sqm' THEN ROUND(val, 2)
                WHEN 'ha' THEN ROUND(val * 10000, 2)
                WHEN 'hektar' THEN ROUND(val * 10000, 2)
                WHEN 'hectare' THEN ROUND(val * 10000, 2)
                WHEN 'hectares' THEN ROUND(val * 10000, 2)
                WHEN 'are' THEN ROUND(val * 100, 2)
                WHEN 'a' THEN ROUND(val * 100, 2)
                WHEN 'bata' THEN ROUND(val * 14, 2)
                WHEN 'tumbak' THEN ROUND(val * 14, 2)
                WHEN 'ru' THEN ROUND(val * 14, 2)
                ELSE ROUND(val, 2)
            END;
        END;
        $$ LANGUAGE plpgsql IMMUTABLE;
        """
    )

    legal_status_enum.create(bind, checkfirst=True)
    document_type_enum.create(bind, checkfirst=True)
    verification_status_enum.create(bind, checkfirst=True)
    owner_type_enum.create(bind, checkfirst=True)

    # 1. Tabel spasial: parcels
    op.create_table(
        "parcels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("parcel_code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("desa_kelurahan", sa.String(length=128), nullable=True),
        sa.Column("kecamatan", sa.String(length=128), nullable=True),
        sa.Column("kabupaten_kota", sa.String(length=128), nullable=True),
        sa.Column(
            "provinsi",
            sa.String(length=128),
            nullable=True,
            server_default="Jawa Barat",
        ),
        sa.Column("alamat_lengkap", sa.Text(), nullable=True),
        sa.Column(
            "legal_status",
            legal_status_enum,
            nullable=False,
            server_default="CLEAR",
        ),
        sa.Column("area_m2", sa.Numeric(14, 2), nullable=True),
        sa.Column("gis_area_m2", sa.Numeric(14, 2), nullable=True),
        sa.Column("original_area_value", sa.Numeric(14, 4), nullable=True),
        sa.Column(
            "original_area_unit",
            sa.String(length=32),
            nullable=True,
            server_default="m2",
        ),
        sa.Column(
            "geom",
            geoalchemy2.Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                spatial_index=False,
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("parcel_code", name="uq_parcels_parcel_code"),
    )
    op.create_index("ix_parcels_parcel_code", "parcels", ["parcel_code"], unique=True)
    op.create_index("ix_parcels_desa_kelurahan", "parcels", ["desa_kelurahan"])
    op.create_index("ix_parcels_kecamatan", "parcels", ["kecamatan"])
    op.create_index("ix_parcels_kabupaten_kota", "parcels", ["kabupaten_kota"])
    op.create_index("ix_parcels_legal_status", "parcels", ["legal_status"])
    op.create_index(
        "idx_parcels_geom",
        "parcels",
        ["geom"],
        postgresql_using="gist",
    )

    # 2. Tabel tabular: documents (mendukung orphaned records via parcel_id NULLABLE)
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "parcel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parcels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("document_number", sa.String(length=128), nullable=False),
        sa.Column("document_type", document_type_enum, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("issuing_office", sa.String(length=255), nullable=True),
        sa.Column(
            "legal_status",
            legal_status_enum,
            nullable=False,
            server_default="CLEAR",
        ),
        sa.Column("stated_area_m2", sa.Numeric(14, 2), nullable=True),
        sa.Column("original_area_value", sa.Numeric(14, 4), nullable=True),
        sa.Column(
            "original_area_unit",
            sa.String(length=32),
            nullable=True,
            server_default="m2",
        ),
        sa.Column("synology_file_path", sa.String(length=512), nullable=False),
        sa.Column("nextcloud_file_id", sa.String(length=128), nullable=True),
        sa.Column("file_checksum_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "verification_status",
            verification_status_enum,
            nullable=False,
            server_default="DRAFT_AI",
        ),
        sa.Column("verified_by", sa.String(length=128), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_documents_parcel_id", "documents", ["parcel_id"])
    op.create_index("ix_documents_document_number", "documents", ["document_number"])
    op.create_index("ix_documents_legal_status", "documents", ["legal_status"])
    op.create_index(
        "ix_documents_verification_status",
        "documents",
        ["verification_status"],
    )

    # 3. Tabel tabular: owners (riwayat kepemilikan berantai)
    op.create_table(
        "owners",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "parcel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("parcels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("owner_name", sa.String(length=255), nullable=False),
        sa.Column(
            "owner_type",
            owner_type_enum,
            nullable=False,
            server_default="INDIVIDU",
        ),
        sa.Column("identity_number", sa.String(length=64), nullable=True),
        sa.Column(
            "sequence_order",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "is_current_owner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("acquisition_type", sa.String(length=64), nullable=True),
        sa.Column("acquisition_date", sa.Date(), nullable=True),
        sa.Column("transfer_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_owners_parcel_id", "owners", ["parcel_id"])
    op.create_index("ix_owners_document_id", "owners", ["document_id"])
    op.create_index("ix_owners_owner_name", "owners", ["owner_name"])
    op.create_index("ix_owners_is_current_owner", "owners", ["is_current_owner"])

    # 4. Tabel tabular: ocr_raw_data (ekstraksi mentah Claude AI & hasil koreksi HITL)
    op.create_table(
        "ocr_raw_data",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "ocr_engine",
            sa.String(length=64),
            nullable=False,
            server_default="claude-3-5-sonnet",
        ),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column(
            "extracted_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "reviewed_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("confidence_score", sa.Numeric(5, 4), nullable=True),
        sa.Column(
            "verification_status",
            verification_status_enum,
            nullable=False,
            server_default="DRAFT_AI",
        ),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("document_id", name="uq_ocr_raw_data_document_id"),
    )
    op.create_index(
        "ix_ocr_raw_data_document_id",
        "ocr_raw_data",
        ["document_id"],
        unique=True,
    )
    op.create_index(
        "ix_ocr_raw_data_verification_status",
        "ocr_raw_data",
        ["verification_status"],
    )
    op.create_index(
        "idx_ocr_raw_data_extracted_json",
        "ocr_raw_data",
        ["extracted_json"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("idx_ocr_raw_data_extracted_json", table_name="ocr_raw_data")
    op.drop_index("ix_ocr_raw_data_verification_status", table_name="ocr_raw_data")
    op.drop_index("ix_ocr_raw_data_document_id", table_name="ocr_raw_data")
    op.drop_table("ocr_raw_data")

    op.drop_index("ix_owners_is_current_owner", table_name="owners")
    op.drop_index("ix_owners_owner_name", table_name="owners")
    op.drop_index("ix_owners_document_id", table_name="owners")
    op.drop_index("ix_owners_parcel_id", table_name="owners")
    op.drop_table("owners")

    op.drop_index("ix_documents_verification_status", table_name="documents")
    op.drop_index("ix_documents_legal_status", table_name="documents")
    op.drop_index("ix_documents_document_number", table_name="documents")
    op.drop_index("ix_documents_parcel_id", table_name="documents")
    op.drop_table("documents")

    op.drop_index("idx_parcels_geom", table_name="parcels")
    op.drop_index("ix_parcels_legal_status", table_name="parcels")
    op.drop_index("ix_parcels_kabupaten_kota", table_name="parcels")
    op.drop_index("ix_parcels_kecamatan", table_name="parcels")
    op.drop_index("ix_parcels_desa_kelurahan", table_name="parcels")
    op.drop_index("ix_parcels_parcel_code", table_name="parcels")
    op.drop_table("parcels")

    owner_type_enum.drop(bind, checkfirst=True)
    verification_status_enum.drop(bind, checkfirst=True)
    document_type_enum.drop(bind, checkfirst=True)
    legal_status_enum.drop(bind, checkfirst=True)

    op.execute("DROP FUNCTION IF EXISTS normalize_area_to_m2(NUMERIC, TEXT);")
