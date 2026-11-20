"""Pydantic schemas untuk tabel spasial `parcels` dan GeoJSON FeatureCollection."""

from datetime import datetime
from decimal import Decimal
from typing import Any, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import LegalStatus
from app.schemas.document import DocumentRead
from app.schemas.owner import OwnerRead


class GeoJSONGeometry(BaseModel):
    """Geometri standar GeoJSON untuk Polygon atau MultiPolygon (SRID 4326)."""

    type: Literal["Polygon", "MultiPolygon"]
    coordinates: List[Any]


class ParcelBase(BaseModel):
    """Atribut dasar persil tanah LDKH."""

    parcel_code: str = Field(..., min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=255)
    desa_kelurahan: Optional[str] = Field(default=None, max_length=128)
    kecamatan: Optional[str] = Field(default=None, max_length=128)
    kabupaten_kota: Optional[str] = Field(default=None, max_length=128)
    provinsi: Optional[str] = Field(default="Jawa Barat", max_length=128)
    alamat_lengkap: Optional[str] = None
    legal_status: LegalStatus = LegalStatus.CLEAR
    original_area_value: Optional[Decimal] = Field(default=None, ge=0)
    original_area_unit: Optional[str] = Field(default="m2", max_length=32)
    gis_area_m2: Optional[Decimal] = Field(default=None, ge=0)
    notes: Optional[str] = None


class ParcelCreate(ParcelBase):
    """Payload pembuatan persil spasial baru beserta geometri GeoJSON."""

    geometry: GeoJSONGeometry
    orphaned_document_id: Optional[UUID] = Field(
        default=None,
        description="ID dokumen orphaned yang akan langsung ditautkan ke persil baru.",
    )
    orphaned_document_ids: Optional[List[UUID]] = Field(
        default=None,
        description="Daftar ID beberapa dokumen orphaned untuk ditautkan sekaligus ke persil baru (1-to-Many).",
    )


class ParcelUpdate(BaseModel):
    """Payload pembaruan atribut atau geometri persil."""

    parcel_code: Optional[str] = Field(default=None, min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=255)
    desa_kelurahan: Optional[str] = Field(default=None, max_length=128)
    kecamatan: Optional[str] = Field(default=None, max_length=128)
    kabupaten_kota: Optional[str] = Field(default=None, max_length=128)
    provinsi: Optional[str] = Field(default=None, max_length=128)
    alamat_lengkap: Optional[str] = None
    legal_status: Optional[LegalStatus] = None
    area_m2: Optional[Decimal] = Field(default=None, ge=0)
    original_area_value: Optional[Decimal] = Field(default=None, ge=0)
    original_area_unit: Optional[str] = Field(default=None, max_length=32)
    gis_area_m2: Optional[Decimal] = Field(default=None, ge=0)
    notes: Optional[str] = None
    geometry: Optional[GeoJSONGeometry] = None

    # Alias opsional untuk kompatibilitas payload standar
    address: Optional[str] = None
    village: Optional[str] = Field(default=None, max_length=128)
    district: Optional[str] = Field(default=None, max_length=128)
    city: Optional[str] = Field(default=None, max_length=128)
    province: Optional[str] = Field(default=None, max_length=128)
    certificate_area: Optional[Decimal] = Field(default=None, ge=0)


class ParcelRead(ParcelBase):
    """Representasi respons ringkas persil spasial."""

    id: UUID
    area_m2: Optional[Decimal] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ParcelDetailRead(ParcelRead):
    """Representasi detail persil lengkap dengan geometri GeoJSON, dokumen, dan pemilik."""

    geometry: Optional[GeoJSONGeometry] = None
    documents: List[DocumentRead] = Field(default_factory=list)
    owners: List[OwnerRead] = Field(default_factory=list)


class ParcelGeoJSONProperties(BaseModel):
    """Properti fitur GeoJSON untuk pewarnaan tematik MapLibre GL JS & sinkronisasi tabel."""

    id: UUID
    parcel_code: str
    name: Optional[str] = None
    desa_kelurahan: Optional[str] = None
    kecamatan: Optional[str] = None
    kabupaten_kota: Optional[str] = None
    provinsi: Optional[str] = None
    legal_status: LegalStatus
    thematic_color: str
    is_disputed_or_seized: bool
    area_m2: Optional[float] = None
    gis_area_m2: Optional[float] = None
    area_discrepancy_m2: Optional[float] = None
    current_owner_name: Optional[str] = None
    document_count: int = 0
    primary_document_id: Optional[UUID] = None
    primary_document_number: Optional[str] = None


class ParcelGeoJSONFeature(BaseModel):
    """Satu GeoJSON Feature persil tanah."""

    type: Literal["Feature"] = "Feature"
    id: str
    geometry: GeoJSONGeometry
    properties: ParcelGeoJSONProperties


class ParcelGeoJSONFeatureCollection(BaseModel):
    """GeoJSON FeatureCollection hasil konversi PostGIS untuk MapLibre GL JS."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    total_features: int
    features: List[ParcelGeoJSONFeature]


DuplicateStrategy = Literal["skip", "update", "create_new"]
PlannedImportAction = Literal["create", "skip", "update", "create_new", "invalid"]


class ParcelImportFeaturePreview(BaseModel):
    """Ringkasan hasil parsing, validasi, & deteksi duplikat per fitur hasil impor peta."""

    feature_index: int
    parcel_code: str
    resolved_parcel_code: str
    name: Optional[str] = None
    owner_name: Optional[str] = None
    village: Optional[str] = None
    district: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    legal_status: LegalStatus = LegalStatus.CLEAR
    certificate_area_m2: Optional[float] = None
    gis_area_m2: Optional[float] = None
    area_discrepancy_m2: Optional[float] = None
    color_group: Optional[str] = None
    raw_label: Optional[str] = None
    notes: Optional[str] = None
    is_valid: bool = True
    validation_error: Optional[str] = None
    is_duplicate: bool = False
    existing_parcel_id: Optional[UUID] = None
    planned_action: PlannedImportAction = "create"
    geometry: Optional[GeoJSONGeometry] = None


class ParcelImportResponse(BaseModel):
    """Respons endpoint bulk import GeoJSON/KML/KMZ (mode dry-run maupun commit)."""

    dry_run: bool
    duplicate_strategy: DuplicateStrategy
    source_filename: str
    total_features: int
    valid_features: int
    invalid_features: int
    duplicate_count: int
    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    total_certificate_area_m2: float = 0.0
    total_gis_area_m2: float = 0.0
    features: List[ParcelImportFeaturePreview] = Field(default_factory=list)
