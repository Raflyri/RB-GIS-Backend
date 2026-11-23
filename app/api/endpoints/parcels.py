"""Endpoint REST API untuk tabel spasial `parcels` & konversi PostGIS ke GeoJSON."""

import json
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from geoalchemy2 import Geography
from sqlalchemy import Numeric, cast, func, or_
from sqlalchemy.orm import Session, selectinload

from app.core.security import require_admin, require_viewer
from app.db.session import get_db
from app.models import Document, LegalStatus, Owner, Parcel, User
from app.schemas import (
    DuplicateStrategy,
    GeoJSONGeometry,
    ParcelCreate,
    ParcelDetailRead,
    ParcelGeoJSONFeature,
    ParcelGeoJSONFeatureCollection,
    ParcelGeoJSONProperties,
    ParcelImportFeaturePreview,
    ParcelImportResponse,
    ParcelRead,
    ParcelUpdate,
    PlannedImportAction,
)
from app.services.parcel_importer import (
    extract_features_for_import,
    parse_upload_to_raw_features,
)
from app.utils.area_normalizer import normalize_to_m2

router = APIRouter()

# Standar pewarnaan tematik MapLibre GL JS (sesuai skill MapLibre-Thematic-Renderer)
LEGAL_STATUS_COLOR_MAP: dict[LegalStatus, str] = {
    LegalStatus.CLEAR: "#22C55E",
    LegalStatus.SENGKETA: "#EAB308",
    LegalStatus.BEDA_FISIK: "#EAB308",
    LegalStatus.DISITA_BI: "#EF4444",
    LegalStatus.RAMPASAN: "#EF4444",
}

DISPUTED_OR_SEIZED_STATUSES = {
    LegalStatus.SENGKETA,
    LegalStatus.BEDA_FISIK,
    LegalStatus.DISITA_BI,
    LegalStatus.RAMPASAN,
}


def _apply_parcel_filters(
    query,
    legal_status: Optional[LegalStatus] = None,
    desa_kelurahan: Optional[str] = None,
    kecamatan: Optional[str] = None,
    search: Optional[str] = None,
    bbox: Optional[str] = None,
):
    """Menerapkan filter atribut dan filter spasial bounding box (PostGIS GIST)."""
    if legal_status is not None:
        query = query.filter(Parcel.legal_status == legal_status)
    if desa_kelurahan:
        query = query.filter(Parcel.desa_kelurahan.ilike(f"%{desa_kelurahan.strip()}%"))
    if kecamatan:
        query = query.filter(Parcel.kecamatan.ilike(f"%{kecamatan.strip()}%"))
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Parcel.parcel_code.ilike(term),
                Parcel.name.ilike(term),
                Parcel.alamat_lengkap.ilike(term),
                Parcel.desa_kelurahan.ilike(term),
            )
        )
    if bbox:
        try:
            parts = [float(x.strip()) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            min_lon, min_lat, max_lon, max_lat = parts
            if min_lon >= max_lon or min_lat >= max_lat:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Format parameter bbox harus 'min_lon,min_lat,max_lon,max_lat' yang valid.",
            ) from exc

        envelope = func.ST_MakeEnvelope(min_lon, min_lat, max_lon, max_lat, 4326)
        query = query.filter(func.ST_Intersects(Parcel.geom, envelope))

    return query


def _compute_gis_area_m2(db: Session, parcel_id: UUID):
    """Menghitung luas poligon geodesik dalam meter persegi (m2) melalui PostGIS."""
    return (
        db.query(
            func.round(
                cast(func.ST_Area(cast(Parcel.geom, Geography)), Numeric(14, 2)),
                2,
            )
        )
        .filter(Parcel.id == parcel_id)
        .scalar()
    )


def _get_parcel_detail(db: Session, parcel_id: UUID) -> ParcelDetailRead:
    """Memuat detail persil beserta GeoJSON geometry, dokumen, dan riwayat pemilik."""
    row = (
        db.query(Parcel, func.ST_AsGeoJSON(Parcel.geom).label("geojson_str"))
        .options(selectinload(Parcel.documents), selectinload(Parcel.owners))
        .filter(Parcel.id == parcel_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Persil dengan ID {parcel_id} tidak ditemukan.",
        )

    parcel, geojson_str = row
    detail = ParcelDetailRead.model_validate(parcel)
    if geojson_str:
        detail.geometry = GeoJSONGeometry(**json.loads(geojson_str))
    return detail


@router.get("/geojson", response_model=ParcelGeoJSONFeatureCollection)
def get_parcels_geojson(
    legal_status: Optional[LegalStatus] = Query(default=None),
    desa_kelurahan: Optional[str] = Query(default=None),
    kecamatan: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    bbox: Optional[str] = Query(
        default=None,
        description="Bounding box filter: min_lon,min_lat,max_lon,max_lat (SRID 4326)",
    ),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> ParcelGeoJSONFeatureCollection:
    """Mengonversi persil PostGIS menjadi GeoJSON FeatureCollection untuk MapLibre GL JS."""
    query = db.query(Parcel, func.ST_AsGeoJSON(Parcel.geom).label("geojson_str")).options(
        selectinload(Parcel.documents),
        selectinload(Parcel.owners),
    )
    query = _apply_parcel_filters(
        query,
        legal_status=legal_status,
        desa_kelurahan=desa_kelurahan,
        kecamatan=kecamatan,
        search=search,
        bbox=bbox,
    )
    rows = query.order_by(Parcel.parcel_code.asc()).all()

    features: List[ParcelGeoJSONFeature] = []
    for parcel, geojson_str in rows:
        if not geojson_str:
            continue

        current_owner = next(
            (owner.owner_name for owner in parcel.owners if owner.is_current_owner),
            parcel.owners[-1].owner_name if parcel.owners else None,
        )
        primary_doc = parcel.documents[0] if parcel.documents else None

        area_val = float(parcel.area_m2) if parcel.area_m2 is not None else None
        gis_val = float(parcel.gis_area_m2) if parcel.gis_area_m2 is not None else None
        discrepancy = (
            round(area_val - gis_val, 2)
            if area_val is not None and gis_val is not None
            else None
        )

        properties = ParcelGeoJSONProperties(
            id=parcel.id,
            parcel_code=parcel.parcel_code,
            name=parcel.name,
            desa_kelurahan=parcel.desa_kelurahan,
            kecamatan=parcel.kecamatan,
            kabupaten_kota=parcel.kabupaten_kota,
            provinsi=parcel.provinsi,
            legal_status=parcel.legal_status,
            thematic_color=LEGAL_STATUS_COLOR_MAP.get(parcel.legal_status, "#64748B"),
            is_disputed_or_seized=parcel.legal_status in DISPUTED_OR_SEIZED_STATUSES,
            area_m2=area_val,
            gis_area_m2=gis_val,
            area_discrepancy_m2=discrepancy,
            current_owner_name=current_owner,
            document_count=len(parcel.documents),
            primary_document_id=primary_doc.id if primary_doc else None,
            primary_document_number=primary_doc.document_number if primary_doc else None,
        )

        features.append(
            ParcelGeoJSONFeature(
                id=str(parcel.id),
                geometry=GeoJSONGeometry(**json.loads(geojson_str)),
                properties=properties,
            )
        )

    return ParcelGeoJSONFeatureCollection(
        total_features=len(features),
        features=features,
    )


@router.get("", response_model=List[ParcelRead])
@router.get("/", response_model=List[ParcelRead], include_in_schema=False)
def list_parcels(
    legal_status: Optional[LegalStatus] = Query(default=None),
    desa_kelurahan: Optional[str] = Query(default=None),
    kecamatan: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> List[Parcel]:
    """Mengambil daftar data persil tanah LDKH dengan filter opsional."""
    query = _apply_parcel_filters(
        db.query(Parcel),
        legal_status=legal_status,
        desa_kelurahan=desa_kelurahan,
        kecamatan=kecamatan,
        search=search,
    )
    return query.order_by(Parcel.parcel_code.asc()).offset(skip).limit(limit).all()


def _build_geom_expr(geometry: GeoJSONGeometry):
    """Membangun ekspresi PostGIS MultiPolygon 2D SRID 4326 dari geometri GeoJSON."""
    geojson_str = json.dumps(geometry.model_dump())
    return func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_MakeValid(func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_str), 4326)),
            3,
        )
    )


def _generate_unique_parcel_code(
    db: Session,
    base_code: str,
    reserved_codes: set[str],
) -> str:
    """Menghasilkan kode persil unik saat strategi duplikat adalah `create_new`."""
    suffix = 2
    while True:
        suffix_str = f"-{suffix}"
        candidate = f"{base_code[: max(1, 64 - len(suffix_str))]}{suffix_str}"
        if candidate not in reserved_codes:
            exists_in_db = (
                db.query(Parcel.id).filter(Parcel.parcel_code == candidate).first()
                is not None
            )
            if not exists_in_db:
                reserved_codes.add(candidate)
                return candidate
        suffix += 1


@router.post("/import", response_model=ParcelImportResponse)
async def import_parcels_bulk(
    file: UploadFile = File(...),
    dry_run: bool = Form(default=True),
    duplicate_strategy: DuplicateStrategy = Form(default="skip"),
    default_village: Optional[str] = Form(default=None),
    default_district: Optional[str] = Form(default=None),
    default_city: Optional[str] = Form(default=None),
    default_province: Optional[str] = Form(default="Jawa Barat"),
    default_legal_status: LegalStatus = Form(default=LegalStatus.CLEAR),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> ParcelImportResponse:
    """Mengimpor poligon persil massal dari GeoJSON, KML, atau KMZ (mode dry-run & commit)."""
    content = await file.read()
    filename = file.filename or "upload.geojson"

    try:
        raw_features = parse_upload_to_raw_features(filename, content)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    extracted = extract_features_for_import(
        raw_features,
        default_village=default_village,
        default_district=default_district,
        default_city=default_city,
        default_province=default_province,
        default_legal_status=default_legal_status,
    )

    candidate_codes = {item.parcel_code for item in extracted}
    existing_by_code: dict[str, Parcel] = {}
    if candidate_codes:
        existing_rows = (
            db.query(Parcel)
            .options(selectinload(Parcel.owners))
            .filter(Parcel.parcel_code.in_(candidate_codes))
            .all()
        )
        existing_by_code = {row.parcel_code: row for row in existing_rows}

    reserved_codes = set(existing_by_code.keys())
    seen_in_batch: set[str] = set()
    batch_created_by_code: dict[str, Parcel] = {}

    previews: List[ParcelImportFeaturePreview] = []
    valid_features = 0
    invalid_features = 0
    duplicate_count = 0
    created_count = 0
    updated_count = 0
    skipped_count = 0
    total_cert_area = 0.0
    total_gis_area = 0.0

    for item in extracted:
        if not item.is_valid or item.geometry is None:
            invalid_features += 1
            previews.append(
                ParcelImportFeaturePreview(
                    feature_index=item.feature_index,
                    parcel_code=item.parcel_code,
                    resolved_parcel_code=item.parcel_code,
                    name=item.name,
                    owner_name=item.owner_name,
                    village=item.village,
                    district=item.district,
                    city=item.city,
                    province=item.province,
                    legal_status=item.legal_status,
                    certificate_area_m2=item.certificate_area_m2,
                    gis_area_m2=item.gis_area_m2,
                    area_discrepancy_m2=item.area_discrepancy_m2,
                    color_group=item.color_group,
                    raw_label=item.raw_label,
                    notes=item.notes,
                    is_valid=False,
                    validation_error=item.validation_error,
                    is_duplicate=False,
                    existing_parcel_id=None,
                    planned_action="invalid",
                    geometry=None,
                )
            )
            continue

        valid_features += 1
        existing_parcel = existing_by_code.get(item.parcel_code)
        is_duplicate = existing_parcel is not None or item.parcel_code in seen_in_batch
        resolved_code = item.parcel_code
        planned_action: PlannedImportAction = "create"

        if is_duplicate:
            duplicate_count += 1
            if duplicate_strategy == "skip":
                planned_action = "skip"
            elif duplicate_strategy == "update":
                planned_action = "update"
            else:
                planned_action = "create_new"
                resolved_code = _generate_unique_parcel_code(
                    db, item.parcel_code, reserved_codes
                )
        else:
            seen_in_batch.add(item.parcel_code)
            reserved_codes.add(item.parcel_code)

        final_gis_area = item.gis_area_m2
        final_discrepancy = item.area_discrepancy_m2

        if not dry_run:
            cert_area_dec = (
                Decimal(str(item.certificate_area_m2))
                if item.certificate_area_m2 is not None
                else None
            )
            gis_area_dec = (
                Decimal(str(item.gis_area_m2)) if item.gis_area_m2 is not None else None
            )

            if planned_action in ("create", "create_new"):
                parcel = Parcel(
                    parcel_code=resolved_code,
                    name=item.name,
                    desa_kelurahan=item.village,
                    kecamatan=item.district,
                    kabupaten_kota=item.city,
                    provinsi=item.province,
                    legal_status=item.legal_status,
                    area_m2=cert_area_dec,
                    gis_area_m2=gis_area_dec,
                    original_area_value=cert_area_dec,
                    original_area_unit="m2",
                    geom=_build_geom_expr(item.geometry),
                    notes=item.notes,
                )
                db.add(parcel)
                db.flush()

                computed_area = _compute_gis_area_m2(db, parcel.id)
                if computed_area is not None:
                    parcel.gis_area_m2 = computed_area
                    final_gis_area = float(computed_area)
                if parcel.area_m2 is None and parcel.gis_area_m2 is not None:
                    parcel.area_m2 = parcel.gis_area_m2

                if item.owner_name:
                    db.add(
                        Owner(
                            parcel_id=parcel.id,
                            owner_name=item.owner_name,
                            sequence_order=1,
                            is_current_owner=True,
                        )
                    )

                batch_created_by_code[resolved_code] = parcel
                created_count += 1
            elif planned_action == "update":
                target = existing_parcel or batch_created_by_code.get(item.parcel_code)
                if target is not None:
                    target.geom = _build_geom_expr(item.geometry)
                    if item.name:
                        target.name = item.name
                    if item.village:
                        target.desa_kelurahan = item.village
                    if item.district:
                        target.kecamatan = item.district
                    if item.city:
                        target.kabupaten_kota = item.city
                    if item.province:
                        target.provinsi = item.province
                    target.legal_status = item.legal_status
                    if cert_area_dec is not None:
                        target.original_area_value = cert_area_dec
                        target.original_area_unit = "m2"
                        target.area_m2 = cert_area_dec
                    if item.notes:
                        target.notes = item.notes

                    db.flush()
                    computed_area = _compute_gis_area_m2(db, target.id)
                    if computed_area is not None:
                        target.gis_area_m2 = computed_area
                        final_gis_area = float(computed_area)
                    if target.area_m2 is None and target.gis_area_m2 is not None:
                        target.area_m2 = target.gis_area_m2

                    if item.owner_name:
                        has_same_owner = any(
                            o.owner_name.strip().lower() == item.owner_name.strip().lower()
                            for o in target.owners
                        )
                        if not has_same_owner:
                            for existing_owner in target.owners:
                                existing_owner.is_current_owner = False
                            next_seq = (
                                max((o.sequence_order for o in target.owners), default=0) + 1
                            )
                            db.add(
                                Owner(
                                    parcel_id=target.id,
                                    owner_name=item.owner_name,
                                    sequence_order=next_seq,
                                    is_current_owner=True,
                                )
                            )

                    updated_count += 1
                else:
                    skipped_count += 1
            else:
                skipped_count += 1

            if item.certificate_area_m2 is not None and final_gis_area is not None:
                final_discrepancy = round(item.certificate_area_m2 - final_gis_area, 2)

        if item.certificate_area_m2 is not None:
            total_cert_area += item.certificate_area_m2
        if final_gis_area is not None:
            total_gis_area += final_gis_area

        previews.append(
            ParcelImportFeaturePreview(
                feature_index=item.feature_index,
                parcel_code=item.parcel_code,
                resolved_parcel_code=resolved_code,
                name=item.name,
                owner_name=item.owner_name,
                village=item.village,
                district=item.district,
                city=item.city,
                province=item.province,
                legal_status=item.legal_status,
                certificate_area_m2=item.certificate_area_m2,
                gis_area_m2=final_gis_area,
                area_discrepancy_m2=final_discrepancy,
                color_group=item.color_group,
                raw_label=item.raw_label,
                notes=item.notes,
                is_valid=True,
                validation_error=None,
                is_duplicate=is_duplicate,
                existing_parcel_id=existing_parcel.id if existing_parcel else None,
                planned_action=planned_action,
                geometry=item.geometry,
            )
        )

    if not dry_run:
        db.commit()

    return ParcelImportResponse(
        dry_run=dry_run,
        duplicate_strategy=duplicate_strategy,
        source_filename=filename,
        total_features=len(extracted),
        valid_features=valid_features,
        invalid_features=invalid_features,
        duplicate_count=duplicate_count,
        created_count=created_count,
        updated_count=updated_count,
        skipped_count=skipped_count,
        total_certificate_area_m2=round(total_cert_area, 2),
        total_gis_area_m2=round(total_gis_area, 2),
        features=previews,
    )


@router.get("/{parcel_id}", response_model=ParcelDetailRead)
def get_parcel(
    parcel_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> ParcelDetailRead:
    """Mengambil detail lengkap satu persil tanah termasuk dokumen dan riwayat pemilik."""
    return _get_parcel_detail(db, parcel_id)


@router.post("", response_model=ParcelDetailRead, status_code=status.HTTP_201_CREATED)
@router.post("/", response_model=ParcelDetailRead, status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_parcel(
    payload: ParcelCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> ParcelDetailRead:
    """Membuat persil spasial baru (termasuk dari digitasi browser) dengan normalisasi luasan ke m2."""
    existing = (
        db.query(Parcel)
        .filter(Parcel.parcel_code == payload.parcel_code.strip())
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Kode persil '{payload.parcel_code}' sudah terdaftar.",
        )

    target_doc_ids: List[UUID] = []
    if payload.orphaned_document_id is not None:
        target_doc_ids.append(payload.orphaned_document_id)
    if payload.orphaned_document_ids:
        for doc_id in payload.orphaned_document_ids:
            if doc_id not in target_doc_ids:
                target_doc_ids.append(doc_id)

    linked_docs: List[Document] = []
    if target_doc_ids:
        linked_docs = (
            db.query(Document).filter(Document.id.in_(target_doc_ids)).all()
        )
        found_ids = {doc.id for doc in linked_docs}
        missing = [str(doc_id) for doc_id in target_doc_ids if doc_id not in found_ids]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dokumen dengan ID {', '.join(missing)} tidak ditemukan.",
            )

    primary_doc: Optional[Document] = linked_docs[0] if linked_docs else None

    original_area_val = payload.original_area_value
    original_area_unit = payload.original_area_unit
    try:
        if original_area_val is not None:
            normalized_area = normalize_to_m2(original_area_val, original_area_unit)
        elif primary_doc is not None:
            normalized_area = primary_doc.stated_area_m2
            original_area_val = primary_doc.original_area_value
            original_area_unit = primary_doc.original_area_unit
        else:
            normalized_area = None
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    geojson_str = json.dumps(payload.geometry.model_dump())
    geom_expr = func.ST_Multi(
        func.ST_CollectionExtract(
            func.ST_MakeValid(func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_str), 4326)),
            3,
        )
    )

    parcel = Parcel(
        parcel_code=payload.parcel_code.strip(),
        name=payload.name,
        desa_kelurahan=payload.desa_kelurahan,
        kecamatan=payload.kecamatan,
        kabupaten_kota=payload.kabupaten_kota,
        provinsi=payload.provinsi,
        alamat_lengkap=payload.alamat_lengkap,
        legal_status=payload.legal_status,
        area_m2=normalized_area,
        gis_area_m2=payload.gis_area_m2,
        original_area_value=original_area_val,
        original_area_unit=original_area_unit,
        geom=geom_expr,
        notes=payload.notes,
    )
    db.add(parcel)
    db.flush()

    if parcel.gis_area_m2 is None:
        parcel.gis_area_m2 = _compute_gis_area_m2(db, parcel.id)

    if parcel.area_m2 is None and parcel.gis_area_m2 is not None:
        parcel.area_m2 = parcel.gis_area_m2

    if linked_docs:
        for doc in linked_docs:
            doc.parcel_id = parcel.id
        db.query(Owner).filter(
            Owner.document_id.in_([doc.id for doc in linked_docs]),
            Owner.parcel_id.is_(None),
        ).update({"parcel_id": parcel.id}, synchronize_session=False)

    db.commit()
    return _get_parcel_detail(db, parcel.id)


@router.patch("/{parcel_id}", response_model=ParcelDetailRead)
@router.put("/{parcel_id}", response_model=ParcelDetailRead)
def update_parcel(
    parcel_id: UUID,
    payload: ParcelUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> ParcelDetailRead:
    """Memperbarui atribut tabular maupun geometri poligon persil tanah tanpa memutus dokumen tertaut."""
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if parcel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Persil dengan ID {parcel_id} tidak ditemukan.",
        )

    update_data = payload.model_dump(exclude_unset=True)

    alias_map = {
        "address": "alamat_lengkap",
        "village": "desa_kelurahan",
        "district": "kecamatan",
        "city": "kabupaten_kota",
        "province": "provinsi",
        "certificate_area": "original_area_value",
    }
    for alias_key, target_key in alias_map.items():
        if alias_key in update_data:
            alias_val = update_data.pop(alias_key)
            if target_key not in update_data:
                update_data[target_key] = alias_val

    if "parcel_code" in update_data:
        raw_code = update_data["parcel_code"]
        new_code = raw_code.strip() if raw_code else ""
        if not new_code:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Kode persil tidak boleh kosong.",
            )
        duplicate = (
            db.query(Parcel)
            .filter(Parcel.parcel_code == new_code, Parcel.id != parcel_id)
            .first()
        )
        if duplicate:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Kode persil '{new_code}' sudah digunakan persil lain.",
            )
        update_data["parcel_code"] = new_code

    geometry_payload = update_data.pop("geometry", None)
    if geometry_payload is not None:
        geojson_str = json.dumps(geometry_payload)
        parcel.geom = func.ST_Multi(
            func.ST_CollectionExtract(
                func.ST_MakeValid(
                    func.ST_SetSRID(func.ST_GeomFromGeoJSON(geojson_str), 4326)
                ),
                3,
            )
        )

    for field, value in update_data.items():
        setattr(parcel, field, value)

    if "original_area_value" in update_data or "original_area_unit" in update_data:
        if parcel.original_area_value is not None:
            try:
                parcel.area_m2 = normalize_to_m2(
                    parcel.original_area_value, parcel.original_area_unit or "m2"
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(exc),
                ) from exc
        elif "area_m2" not in update_data:
            parcel.area_m2 = None

    db.flush()
    if geometry_payload is not None and "gis_area_m2" not in update_data:
        parcel.gis_area_m2 = _compute_gis_area_m2(db, parcel.id)

    if parcel.area_m2 is None and parcel.gis_area_m2 is not None:
        parcel.area_m2 = parcel.gis_area_m2

    db.commit()
    return _get_parcel_detail(db, parcel.id)


@router.delete("/{parcel_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_parcel(
    parcel_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> Response:
    """Menghapus persil spasial secara aman tanpa menghapus file fisik PDF di Synology.

    Dokumen tertaut dilepas relasinya (parcel_id = NULL) sehingga kembali menjadi
    orphaned records, catatan pemilik tanpa dokumen dihapus, dan file PDF di Vault
    tetap utuh.
    """
    parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
    if parcel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Persil dengan ID {parcel_id} tidak ditemukan.",
        )

    db.query(Document).filter(Document.parcel_id == parcel_id).update(
        {"parcel_id": None}, synchronize_session=False
    )
    db.query(Owner).filter(
        Owner.parcel_id == parcel_id, Owner.document_id.is_not(None)
    ).update({"parcel_id": None}, synchronize_session=False)
    db.query(Owner).filter(
        Owner.parcel_id == parcel_id, Owner.document_id.is_(None)
    ).delete(synchronize_session=False)

    db.expire(parcel)
    db.delete(parcel)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
