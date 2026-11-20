"""Layanan AI-Powered OCR Search menggunakan Google Gemini API (google-genai SDK)."""

from typing import Any, Dict, List, Optional
from uuid import UUID

from google import genai
from sqlalchemy import Text, and_, cast, func, or_
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.models import Document, OcrRawData, Owner, Parcel
from app.schemas.search import (
    AISearchOrphanedMatch,
    AISearchParcelMatch,
    AISearchQueryPlan,
    AISearchResponse,
)

LDKH_SCHEMA_CONTEXT = """
Kamu adalah mesin penerjemah kueri pencarian (Natural Language to Structured Query) untuk Sistem Web GIS Digitalisasi Tanah LDKH (PostGIS + PostgreSQL).

Skema database yang tersedia:
1. Tabel `parcels` (spasial poligon tanah):
   - `parcel_code` (VARCHAR), `name` (VARCHAR)
   - `desa_kelurahan`, `kecamatan`, `kabupaten_kota`, `provinsi`, `alamat_lengkap`
   - `legal_status` ENUM: `CLEAR`, `SENGKETA`, `BEDA_FISIK`, `DISITA_BI`, `RAMPASAN`
   - `area_m2` (luas surat dalam m2), `gis_area_m2` (luas pengukuran poligon GIS dalam m2), `notes` (TEXT)
2. Tabel `documents` (dokumen legal pertanahan, berelasi ke `parcels` via `parcel_id` nullable):
   - `document_number` (mis. SHM-..., AJB-..., BA-SITA-BI/...)
   - `document_type` ENUM: `SHM`, `SHGB`, `AJB`, `GIRIK`, `LETTER_C`, `SPH`, `PUTUSAN_PENGADILAN`, `BERITA_ACARA_SITA`, `LAINNYA`
   - `title`, `issuing_office`, `legal_status` ENUM, `stated_area_m2`
   - `verification_status` ENUM: `DRAFT_AI`, `NEEDS_REVIEW`, `VERIFIED`
3. Tabel `owners` (riwayat kepemilikan berantai):
   - `owner_name`, `owner_type` ENUM (`INDIVIDU`, `KORPORASI`, `INSTANSI_PEMERINTAH`, `BANK_INDONESIA`, `AHLI_WARIS`), `is_current_owner`, `acquisition_type`, `transfer_notes`
4. Tabel `ocr_raw_data` (teks penuh hasil OCR dokumen legal):
   - `raw_text` (TEXT penuh dari scan PDF), `extracted_json` (JSONB), `reviewed_json` (JSONB), `review_notes` (TEXT), `verification_status` ENUM

Aturan ekstraksi filter:
- Petakan konsep hukum ke ENUM yang tepat:
  - "disita BI", "sitaan Bank Indonesia", "BLBI", "sita BI" -> `legal_statuses: ["DISITA_BI"]`
  - "rampasan", "dirampas negara" -> `legal_statuses: ["RAMPASAN"]`
  - "sengketa", "bermasalah", "klaim" -> `legal_statuses: ["SENGKETA"]`
  - "beda fisik", "selisih luas", "beda luas" -> `legal_statuses: ["BEDA_FISIK"]` (dan `area_discrepancy_only: true` jika menanyakan selisih luas)
  - "clear", "aman", "bersih", "selesai" -> `legal_statuses: ["CLEAR"]`
- Petakan jenis surat ("girik", "letter c", "shm", "shgb", "ajb", "sph", "berita acara sita", "putusan pengadilan") ke `document_types`.
- Petakan status review ("belum diverifikasi", "perlu review", "draft ai", "terverifikasi") ke `verification_statuses`.
- Konversi satuan luas ke meter persegi (`m2`): 1 Ha / hektar = 10000 m2, 1 are = 100 m2, 1 bata / tumbak = 14 m2.
- JANGAN memasukkan kata umum / stopword seperti "tanah", "lahan", "persil", "aset", "yang", "di", "ke", "dari", "semua", "tampilkan", "cari", "berstatus", "dokumen", "surat" ke dalam `location_keywords`, `owner_keywords`, atau `text_keywords`.
- Masukkan hanya nama lokasi spesifik ke `location_keywords`, nama orang/badan ke `owner_keywords`, dan istilah/nomor unik yang perlu dicari di teks OCR ke `text_keywords`.
- Berikan `explanation` singkat dan jelas dalam Bahasa Indonesia.
""".strip()


class GeminiNotConfiguredError(RuntimeError):
    """Dilempar saat GEMINI_API_KEY belum tersedia di environment."""


class GeminiUpstreamError(RuntimeError):
    """Dilempar saat pemanggilan Gemini API gagal."""


def _resolve_refs(node: Any, defs: Dict[str, Any]) -> Any:
    """Menguraikan referensi $defs/$ref menjadi JSON Schema inline."""
    if isinstance(node, dict):
        if "$ref" in node and isinstance(node["$ref"], str):
            ref_name = node["$ref"].rsplit("/", 1)[-1]
            if ref_name in defs:
                merged = {k: v for k, v in node.items() if k != "$ref"}
                target = _resolve_refs(defs[ref_name], defs)
                if isinstance(target, dict):
                    return {**target, **merged}
                return target
        return {k: _resolve_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_resolve_refs(item, defs) for item in node]
    return node


def build_query_plan_json_schema() -> Dict[str, Any]:
    """Menghasilkan JSON Schema inline untuk AISearchQueryPlan."""
    raw = AISearchQueryPlan.model_json_schema()
    defs = raw.get("$defs", {})
    return _resolve_refs(raw, defs)


def interpret_query_with_gemini(query: str) -> AISearchQueryPlan:
    """Mengonversi query pencarian alami menjadi AISearchQueryPlan via Gemini API."""
    api_key = (settings.GEMINI_API_KEY or "").strip()
    if not api_key:
        raise GeminiNotConfiguredError(
            "GEMINI_API_KEY belum dikonfigurasi di environment backend."
        )

    client = genai.Client(api_key=api_key)
    try:
        interaction = client.interactions.create(
            model=settings.GEMINI_MODEL,
            system_instruction=LDKH_SCHEMA_CONTEXT,
            input=f"Kueri pengguna: {query.strip()}",
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": build_query_plan_json_schema(),
            },
            store=False,
        )
    except Exception as exc:  # pragma: no cover - bergantung jaringan/API eksternal
        raise GeminiUpstreamError(f"Gagal memanggil Gemini API: {exc}") from exc

    output_text = (interaction.output_text or "").strip()
    if not output_text:
        raise GeminiUpstreamError("Gemini API mengembalikan respons kosong.")

    try:
        return AISearchQueryPlan.model_validate_json(output_text)
    except Exception as exc:
        raise GeminiUpstreamError(
            f"Respons Gemini tidak sesuai skema terstruktur: {exc}"
        ) from exc


def _keyword_clause(columns: List[Any], keyword: str):
    """Membangun ekspresi OR ILIKE lintas kolom untuk satu kata kunci."""
    pattern = f"%{keyword.strip()}%"
    return or_(*[col.ilike(pattern) for col in columns])


def execute_search_plan(
    db: Session,
    query: str,
    plan: AISearchQueryPlan,
) -> AISearchResponse:
    """Mengeksekusi AISearchQueryPlan ke database PostGIS dan mengembalikan daftar parcel_id."""
    parcel_query = (
        db.query(Parcel)
        .outerjoin(Document, Document.parcel_id == Parcel.id)
        .outerjoin(OcrRawData, OcrRawData.document_id == Document.id)
        .outerjoin(Owner, or_(Owner.parcel_id == Parcel.id, Owner.document_id == Document.id))
    )

    conditions = []

    if plan.legal_statuses:
        conditions.append(
            or_(
                Parcel.legal_status.in_(plan.legal_statuses),
                Document.legal_status.in_(plan.legal_statuses),
            )
        )

    if plan.document_types:
        conditions.append(Document.document_type.in_(plan.document_types))

    if plan.verification_statuses:
        conditions.append(
            or_(
                Document.verification_status.in_(plan.verification_statuses),
                OcrRawData.verification_status.in_(plan.verification_statuses),
            )
        )

    location_cols = [
        Parcel.desa_kelurahan,
        Parcel.kecamatan,
        Parcel.kabupaten_kota,
        Parcel.provinsi,
        Parcel.alamat_lengkap,
        Parcel.name,
        Parcel.notes,
        Document.title,
        Document.issuing_office,
        OcrRawData.raw_text,
        cast(OcrRawData.extracted_json, Text),
        cast(OcrRawData.reviewed_json, Text),
    ]
    for kw in plan.location_keywords:
        if kw.strip():
            conditions.append(_keyword_clause(location_cols, kw))

    owner_cols = [
        Owner.owner_name,
        Owner.transfer_notes,
        OcrRawData.raw_text,
        cast(OcrRawData.extracted_json, Text),
        cast(OcrRawData.reviewed_json, Text),
    ]
    for kw in plan.owner_keywords:
        if kw.strip():
            conditions.append(_keyword_clause(owner_cols, kw))

    text_cols = [
        OcrRawData.raw_text,
        cast(OcrRawData.extracted_json, Text),
        cast(OcrRawData.reviewed_json, Text),
        OcrRawData.review_notes,
        Document.document_number,
        Document.title,
        Document.issuing_office,
        Parcel.parcel_code,
        Parcel.name,
        Parcel.alamat_lengkap,
        Parcel.notes,
        Owner.owner_name,
        Owner.transfer_notes,
    ]
    for kw in plan.text_keywords:
        if kw.strip():
            conditions.append(_keyword_clause(text_cols, kw))

    if plan.min_area_m2 is not None:
        conditions.append(
            or_(
                Parcel.area_m2 >= plan.min_area_m2,
                Parcel.gis_area_m2 >= plan.min_area_m2,
            )
        )

    if plan.max_area_m2 is not None:
        conditions.append(
            or_(
                Parcel.area_m2 <= plan.max_area_m2,
                Parcel.gis_area_m2 <= plan.max_area_m2,
            )
        )

    if plan.area_discrepancy_only:
        conditions.append(Parcel.area_m2.is_not(None))
        conditions.append(Parcel.gis_area_m2.is_not(None))
        conditions.append(func.abs(Parcel.area_m2 - Parcel.gis_area_m2) > 0.5)

    if conditions:
        parcel_query = parcel_query.filter(and_(*conditions))

    matched_parcels = (
        parcel_query.options(
            selectinload(Parcel.documents).selectinload(Document.ocr_raw_data),
        )
        .distinct()
        .order_by(Parcel.parcel_code.asc())
        .all()
    )

    parcel_ids: List[UUID] = []
    matches: List[AISearchParcelMatch] = []
    for parcel in matched_parcels:
        parcel_ids.append(parcel.id)
        doc_numbers = [doc.document_number for doc in parcel.documents]
        snippet: Optional[str] = None
        for doc in parcel.documents:
            if doc.ocr_raw_data and doc.ocr_raw_data.raw_text:
                snippet = doc.ocr_raw_data.raw_text[:220]
                break
        matches.append(
            AISearchParcelMatch(
                parcel_id=parcel.id,
                parcel_code=parcel.parcel_code,
                name=parcel.name,
                legal_status=parcel.legal_status,
                desa_kelurahan=parcel.desa_kelurahan,
                matched_documents=doc_numbers,
                ocr_snippet=snippet,
            )
        )

    # Cek juga dokumen orphaned (parcel_id IS NULL) agar pencarian OCR tetap informatif.
    orphaned_query = (
        db.query(Document)
        .outerjoin(OcrRawData, OcrRawData.document_id == Document.id)
        .outerjoin(Owner, Owner.document_id == Document.id)
        .filter(Document.parcel_id.is_(None))
    )
    orphan_conditions = []
    if plan.legal_statuses:
        orphan_conditions.append(Document.legal_status.in_(plan.legal_statuses))
    if plan.document_types:
        orphan_conditions.append(Document.document_type.in_(plan.document_types))
    if plan.verification_statuses:
        orphan_conditions.append(
            or_(
                Document.verification_status.in_(plan.verification_statuses),
                OcrRawData.verification_status.in_(plan.verification_statuses),
            )
        )
    orphan_text_cols = [
        OcrRawData.raw_text,
        cast(OcrRawData.extracted_json, Text),
        cast(OcrRawData.reviewed_json, Text),
        OcrRawData.review_notes,
        Document.document_number,
        Document.title,
        Document.issuing_office,
        Owner.owner_name,
        Owner.transfer_notes,
    ]
    for kw in [*plan.location_keywords, *plan.owner_keywords, *plan.text_keywords]:
        if kw.strip():
            orphan_conditions.append(_keyword_clause(orphan_text_cols, kw))
    if plan.min_area_m2 is not None:
        orphan_conditions.append(Document.stated_area_m2 >= plan.min_area_m2)
    if plan.max_area_m2 is not None:
        orphan_conditions.append(Document.stated_area_m2 <= plan.max_area_m2)

    orphaned_matches: List[AISearchOrphanedMatch] = []
    if orphan_conditions and not plan.area_discrepancy_only:
        orphan_rows = (
            orphaned_query.filter(and_(*orphan_conditions))
            .options(selectinload(Document.ocr_raw_data))
            .distinct()
            .order_by(Document.document_number.asc())
            .all()
        )
        for doc in orphan_rows:
            orphaned_matches.append(
                AISearchOrphanedMatch(
                    document_id=doc.id,
                    document_number=doc.document_number,
                    document_type=doc.document_type,
                    title=doc.title,
                    legal_status=doc.legal_status,
                    ocr_snippet=(
                        doc.ocr_raw_data.raw_text[:220]
                        if doc.ocr_raw_data and doc.ocr_raw_data.raw_text
                        else None
                    ),
                )
            )

    return AISearchResponse(
        query=query,
        model_used=settings.GEMINI_MODEL,
        explanation=plan.explanation,
        parsed_filters=plan,
        parcel_ids=parcel_ids,
        total_matched=len(parcel_ids),
        matches=matches,
        orphaned_matches=orphaned_matches,
    )


def run_ai_search(db: Session, query: str) -> AISearchResponse:
    """Orkestrasi lengkap AI Search: Gemini NLP -> Structured Query Plan -> PostGIS."""
    plan = interpret_query_with_gemini(query)
    return execute_search_plan(db, query, plan)
