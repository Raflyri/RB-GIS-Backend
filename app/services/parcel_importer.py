"""Layanan parsing, validasi geometri 2D EPSG:4326, dan normalisasi impor massal GeoJSON/KML/KMZ."""

from __future__ import annotations

import io
import json
import math
import re
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.ops import transform

from app.models import LegalStatus
from app.schemas import GeoJSONGeometry

WGS84_RADIUS_M = 6378137.0
ALLOWED_LEGAL_STATUS_NAMES = {item.value: item for item in LegalStatus}


@dataclass
class ExtractedParcelFeature:
    """Representasi hasil parsing satu fitur poligon sebelum pencocokan database."""

    feature_index: int
    parcel_code: str
    name: Optional[str]
    owner_name: Optional[str]
    village: Optional[str]
    district: Optional[str]
    city: Optional[str]
    province: Optional[str]
    legal_status: LegalStatus
    certificate_area_m2: Optional[float]
    gis_area_m2: Optional[float]
    area_discrepancy_m2: Optional[float]
    color_group: Optional[str]
    raw_label: Optional[str]
    notes: Optional[str]
    is_valid: bool
    validation_error: Optional[str]
    geometry: Optional[GeoJSONGeometry]


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_child_text(elem: ET.Element, target_tag: str) -> Optional[str]:
    for child in elem:
        if _local_tag(child.tag) == target_tag and child.text:
            text = child.text.strip()
            if text:
                return text
    return None


def _parse_kml_coordinates(coord_text: str) -> List[List[float]]:
    coords: List[List[float]] = []
    for token in re.split(r"\s+", coord_text.strip()):
        if not token:
            continue
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        if math.isfinite(lon) and math.isfinite(lat):
            coords.append([lon, lat])
    if len(coords) >= 3 and coords[0] != coords[-1]:
        coords.append(list(coords[0]))
    return coords


def _extract_kml_polygon_rings(polygon_elem: ET.Element) -> Optional[List[List[List[float]]]]:
    outer_rings: List[List[List[float]]] = []
    inner_rings: List[List[List[float]]] = []

    for elem in polygon_elem.iter():
        tag = _local_tag(elem.tag)
        if tag == "outerBoundaryIs":
            for coord_el in elem.iter():
                if _local_tag(coord_el.tag) == "coordinates" and coord_el.text:
                    ring = _parse_kml_coordinates(coord_el.text)
                    if len(ring) >= 4:
                        outer_rings.append(ring)
        elif tag == "innerBoundaryIs":
            for coord_el in elem.iter():
                if _local_tag(coord_el.tag) == "coordinates" and coord_el.text:
                    ring = _parse_kml_coordinates(coord_el.text)
                    if len(ring) >= 4:
                        inner_rings.append(ring)

    if not outer_rings:
        return None
    return [outer_rings[0], *inner_rings]


def _parse_kml_extended_data(placemark: ET.Element) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for elem in placemark.iter():
        tag = _local_tag(elem.tag)
        if tag == "Data":
            key = (elem.attrib.get("name") or "").strip()
            val = _find_child_text(elem, "value")
            if key and val is not None:
                props[key] = val
        elif tag == "SimpleData":
            key = (elem.attrib.get("name") or "").strip()
            val = (elem.text or "").strip()
            if key and val:
                props[key] = val
    return props


def _strip_html_tags(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _parse_kml_bytes(kml_bytes: bytes) -> List[Dict[str, Any]]:
    try:
        xml_text = kml_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        xml_text = kml_bytes.decode("latin-1")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Format XML KML tidak valid: {exc}") from exc

    features: List[Dict[str, Any]] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "Placemark":
            continue

        props = _parse_kml_extended_data(elem)
        kml_name = _find_child_text(elem, "name")
        kml_desc = _find_child_text(elem, "description")
        if kml_name and "name" not in props:
            props["name"] = kml_name
        if kml_desc and "description" not in props:
            props["description"] = _strip_html_tags(kml_desc)

        polygons: List[List[List[List[float]]]] = []
        for child in elem.iter():
            if _local_tag(child.tag) == "Polygon":
                rings = _extract_kml_polygon_rings(child)
                if rings:
                    polygons.append(rings)

        geometry: Optional[Dict[str, Any]] = None
        if len(polygons) == 1:
            geometry = {"type": "Polygon", "coordinates": polygons[0]}
        elif len(polygons) > 1:
            geometry = {"type": "MultiPolygon", "coordinates": polygons}

        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": geometry,
            }
        )

    if not features:
        raise ValueError("File KML tidak memiliki elemen <Placemark>.")
    return features


def _parse_kmz_bytes(kmz_bytes: bytes) -> List[Dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(kmz_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError("File KMZ rusak atau bukan arsip ZIP yang valid.") from exc

    with archive:
        kml_candidates = [
            name
            for name in archive.namelist()
            if name.lower().endswith(".kml") and not name.startswith("__MACOSX/")
        ]
        if not kml_candidates:
            raise ValueError("Arsip KMZ tidak mengandung file .kml di dalamnya.")

        preferred = next(
            (name for name in kml_candidates if name.lower().endswith("doc.kml")),
            kml_candidates[0],
        )
        return _parse_kml_bytes(archive.read(preferred))


def _parse_geojson_bytes(raw_bytes: bytes) -> List[Dict[str, Any]]:
    try:
        data = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"File GeoJSON tidak valid: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("Root GeoJSON harus berupa objek JSON.")

    geojson_type = data.get("type")
    if geojson_type == "FeatureCollection":
        features = data.get("features")
        if not isinstance(features, list) or not features:
            raise ValueError("GeoJSON FeatureCollection tidak memiliki fitur.")
        return [f for f in features if isinstance(f, dict)]

    if geojson_type == "Feature":
        return [data]

    if geojson_type in ("Polygon", "MultiPolygon"):
        return [{"type": "Feature", "properties": {}, "geometry": data}]

    raise ValueError(
        f"Tipe GeoJSON '{geojson_type}' tidak didukung. Gunakan FeatureCollection, Feature, Polygon, atau MultiPolygon."
    )


def parse_upload_to_raw_features(filename: str, content: bytes) -> List[Dict[str, Any]]:
    """Membaca file GeoJSON, KML, atau KMZ menjadi daftar dict fitur mentah."""
    if not content:
        raise ValueError("File yang diunggah kosong.")

    lower_name = (filename or "").lower()
    if lower_name.endswith(".kmz") or content[:4] == b"PK\x03\x04":
        return _parse_kmz_bytes(content)
    if lower_name.endswith(".kml") or content.lstrip().startswith(b"<"):
        return _parse_kml_bytes(content)
    if lower_name.endswith(".geojson") or lower_name.endswith(".json") or content.lstrip().startswith(b"{"):
        return _parse_geojson_bytes(content)

    raise ValueError("Format file tidak didukung. Gunakan file .geojson, .json, .kml, atau .kmz.")


def _collect_polygons(geom: Any) -> List[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom] if geom.area > 0 else []
    if isinstance(geom, (MultiPolygon, GeometryCollection)):
        out: List[Polygon] = []
        for sub in geom.geoms:
            out.extend(_collect_polygons(sub))
        return out
    return []


def _ring_area_m2(coords: List[List[float]]) -> float:
    if len(coords) < 4:
        return 0.0
    n = len(coords) - 1
    total = 0.0
    for i in range(n):
        p1 = coords[i]
        p2 = coords[i + 1]
        p3 = coords[(i + 2) % n]
        total += (math.radians(p3[0]) - math.radians(p1[0])) * math.sin(math.radians(p2[1]))
    return abs((total * WGS84_RADIUS_M * WGS84_RADIUS_M) / 2.0)


def _polygon_coords_area_m2(rings: List[List[List[float]]]) -> float:
    if not rings:
        return 0.0
    area = _ring_area_m2(rings[0])
    for hole in rings[1:]:
        area -= _ring_area_m2(hole)
    return max(0.0, area)


def compute_geodesic_area_m2(geometry: GeoJSONGeometry) -> float:
    """Menghitung estimasi luas geodesik WGS84 (m²) dari geometri GeoJSON 2D."""
    if geometry.type == "Polygon":
        return round(_polygon_coords_area_m2(geometry.coordinates), 2)
    total = sum(_polygon_coords_area_m2(poly) for poly in geometry.coordinates)
    return round(total, 2)


def _to_plain_coords(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return [_to_plain_coords(item) for item in obj]
    if isinstance(obj, list):
        return [_to_plain_coords(item) for item in obj]
    if isinstance(obj, (int, float)):
        return float(obj)
    return obj


def validate_and_normalize_geometry(
    raw_geom: Any,
) -> Tuple[Optional[GeoJSONGeometry], Optional[float], Optional[str]]:
    """Memvalidasi geometri, membuang dimensi Z (3D->2D), memperbaiki ring, dan menghitung luas m²."""
    if not isinstance(raw_geom, dict):
        return None, None, "Fitur tidak memiliki objek geometri Polygon/MultiPolygon."

    try:
        geom = shape(raw_geom)
    except Exception as exc:
        return None, None, f"Geometri tidak dapat diparsing: {exc}"

    if geom.is_empty:
        return None, None, "Geometri kosong."

    try:
        geom_2d = transform(lambda x, y, z=None: (float(x), float(y)), geom)
        repaired = make_valid(geom_2d)
    except Exception as exc:
        return None, None, f"Gagal memvalidasi geometri: {exc}"

    polygons = _collect_polygons(repaired)
    if not polygons:
        return None, None, "Geometri tidak menghasilkan Polygon tertutup yang valid."

    normalized_geom: Polygon | MultiPolygon = (
        polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    )

    minx, miny, maxx, maxy = normalized_geom.bounds
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        return None, None, "Geometri mengandung nilai koordinat non-finite."
    if minx < -180.0 or maxx > 180.0 or miny < -90.0 or maxy > 90.0:
        return (
            None,
            None,
            "Koordinat berada di luar rentang EPSG:4326 (lon -180..180, lat -90..90).",
        )

    geom_mapping = mapping(normalized_geom)
    geo_model = GeoJSONGeometry(
        type=geom_mapping["type"],
        coordinates=_to_plain_coords(geom_mapping["coordinates"]),
    )
    gis_area = compute_geodesic_area_m2(geo_model)
    if gis_area <= 0:
        return None, None, "Luas geometri hasil normalisasi bernilai 0 m²."

    return geo_model, gis_area, None


def _get_prop(props: Dict[str, Any], *keys: str) -> Optional[str]:
    lower_lookup = {str(k).strip().lower(): v for k, v in props.items()}
    for key in keys:
        val = lower_lookup.get(key.lower())
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


def _parse_area_number(raw_val: Any) -> Optional[float]:
    if raw_val is None or isinstance(raw_val, bool):
        return None
    if isinstance(raw_val, (int, float)):
        val = float(raw_val)
        return round(val, 2) if math.isfinite(val) and val >= 0 else None

    text = str(raw_val).strip().lower()
    if not text:
        return None
    text = re.sub(r"(m2|m²|meter\s*persegi|sqm)", "", text).strip()
    match = re.search(r"[-+]?\d[\d.,]*", text)
    if not match:
        return None
    token = match.group(0)

    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        parts = token.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            token = token.replace(",", ".")
        elif len(parts) > 2:
            token = token.replace(",", "")
        else:
            token = token.replace(",", ".")

    try:
        val = float(token)
    except ValueError:
        return None
    return round(val, 2) if math.isfinite(val) and val >= 0 else None


def _normalize_legal_status(raw_status: Optional[str], fallback: LegalStatus) -> LegalStatus:
    if not raw_status:
        return fallback
    normalized = raw_status.strip().upper().replace(" ", "_")
    return ALLOWED_LEGAL_STATUS_NAMES.get(normalized, fallback)


def extract_features_for_import(
    raw_features: List[Dict[str, Any]],
    *,
    default_village: Optional[str] = None,
    default_district: Optional[str] = None,
    default_city: Optional[str] = None,
    default_province: Optional[str] = "Jawa Barat",
    default_legal_status: LegalStatus = LegalStatus.CLEAR,
) -> List[ExtractedParcelFeature]:
    """Mengonversi fitur mentah GeoJSON/KML/KMZ menjadi daftar fitur tervalidasi siap pratinjau/simpan."""
    results: List[ExtractedParcelFeature] = []

    for idx, feature in enumerate(raw_features, start=1):
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}

        raw_code = _get_prop(
            props,
            "parcel_code",
            "kode_persil",
            "code",
            "kode",
            "nib",
            "no_hak",
            "nomor_hak",
            "id_persil",
            "name",
        )
        parcel_code = (raw_code or f"IMP-{idx:03d}")[:64]

        name = _get_prop(props, "name", "nama", "label", "title")
        owner_name = _get_prop(
            props, "owner_name", "pemilik", "nama_pemilik", "owner", "atas_nama"
        )
        village = _get_prop(props, "desa_kelurahan", "village", "desa", "kelurahan") or (
            default_village.strip() if default_village and default_village.strip() else None
        )
        district = _get_prop(props, "kecamatan", "district") or (
            default_district.strip() if default_district and default_district.strip() else None
        )
        city = _get_prop(props, "kabupaten_kota", "city", "kabupaten", "kota") or (
            default_city.strip() if default_city and default_city.strip() else None
        )
        province = _get_prop(props, "provinsi", "province") or (
            default_province.strip() if default_province and default_province.strip() else None
        )
        legal_status = _normalize_legal_status(
            _get_prop(props, "legal_status", "status_hukum", "status"),
            default_legal_status,
        )

        raw_area = None
        for key in (
            "certificate_area",
            "certificate_area_m2",
            "area_m2",
            "stated_area_m2",
            "luas_sertifikat",
            "luas_m2",
            "luas",
        ):
            val = _get_prop(props, key)
            if val is not None:
                raw_area = val
                break
        certificate_area_m2 = _parse_area_number(raw_area)

        color_group = _get_prop(props, "color_group", "kelompok_warna")
        raw_label = _get_prop(props, "raw_label", "raw_text")
        notes = _get_prop(props, "notes", "catatan", "description", "keterangan")
        if not notes and raw_label:
            notes = f"Sumber label impor: {raw_label}"

        geom_model, gis_area_m2, validation_error = validate_and_normalize_geometry(
            feature.get("geometry")
        )
        discrepancy = (
            round(certificate_area_m2 - gis_area_m2, 2)
            if certificate_area_m2 is not None and gis_area_m2 is not None
            else None
        )

        results.append(
            ExtractedParcelFeature(
                feature_index=idx,
                parcel_code=parcel_code,
                name=name[:255] if name else None,
                owner_name=owner_name[:255] if owner_name else None,
                village=village[:128] if village else None,
                district=district[:128] if district else None,
                city=city[:128] if city else None,
                province=province[:128] if province else None,
                legal_status=legal_status,
                certificate_area_m2=certificate_area_m2,
                gis_area_m2=gis_area_m2,
                area_discrepancy_m2=discrepancy,
                color_group=color_group,
                raw_label=raw_label,
                notes=notes,
                is_valid=geom_model is not None,
                validation_error=validation_error,
                geometry=geom_model,
            )
        )

    return results
