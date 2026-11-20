"""Pydantic schemas untuk fitur AI-Powered OCR Search berbasis Google Gemini API."""

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.enums import DocumentType, LegalStatus, VerificationStatus


class AISearchRequest(BaseModel):
    """Payload permintaan pencarian bahasa alami dari Frontend."""

    query: str = Field(
        ...,
        min_length=2,
        max_length=500,
        description="Pertanyaan / prompt pencarian alami, mis. 'Tanah di Sukaharja yang disita BI'.",
    )


class AISearchQueryPlan(BaseModel):
    """Parameter query terstruktur hasil interpretasi Gemini API atas skema LDKH."""

    legal_statuses: List[LegalStatus] = Field(
        default_factory=list,
        description="Filter status hukum persil/dokumen (CLEAR, SENGKETA, BEDA_FISIK, DISITA_BI, RAMPASAN).",
    )
    document_types: List[DocumentType] = Field(
        default_factory=list,
        description="Filter jenis dokumen legal (SHM, SHGB, AJB, GIRIK, LETTER_C, SPH, PUTUSAN_PENGADILAN, BERITA_ACARA_SITA, LAINNYA).",
    )
    verification_statuses: List[VerificationStatus] = Field(
        default_factory=list,
        description="Filter status verifikasi OCR HITL (DRAFT_AI, NEEDS_REVIEW, VERIFIED).",
    )
    location_keywords: List[str] = Field(
        default_factory=list,
        description="Kata kunci lokasi geografis/administratif (desa, kelurahan, kecamatan, kampung, blok, jalan).",
    )
    owner_keywords: List[str] = Field(
        default_factory=list,
        description="Kata kunci nama pemilik, ahli waris, korporasi, atau pihak terkait.",
    )
    text_keywords: List[str] = Field(
        default_factory=list,
        description="Kata kunci spesifik non-stopword pada teks OCR, nomor surat, atau catatan (mis. BLBI, 1147, BPPN).",
    )
    min_area_m2: Optional[float] = Field(
        default=None,
        ge=0,
        description="Batas minimum luas tanah dalam meter persegi (m2).",
    )
    max_area_m2: Optional[float] = Field(
        default=None,
        ge=0,
        description="Batas maksimum luas tanah dalam meter persegi (m2).",
    )
    area_discrepancy_only: bool = Field(
        default=False,
        description="Bernilai true jika pengguna mencari persil dengan selisih/beda luas surat dan fisik GIS.",
    )
    explanation: str = Field(
        ...,
        description="Penjelasan singkat dalam bahasa Indonesia mengenai filter yang diturunkan dari query.",
    )


class AISearchParcelMatch(BaseModel):
    """Ringkasan persil spasial yang cocok dengan hasil AI Search."""

    parcel_id: UUID
    parcel_code: str
    name: Optional[str] = None
    legal_status: LegalStatus
    desa_kelurahan: Optional[str] = None
    matched_documents: List[str] = Field(default_factory=list)
    ocr_snippet: Optional[str] = None


class AISearchOrphanedMatch(BaseModel):
    """Ringkasan dokumen orphaned (belum punya poligon) yang cocok dengan hasil AI Search."""

    document_id: UUID
    document_number: str
    document_type: DocumentType
    title: Optional[str] = None
    legal_status: LegalStatus
    ocr_snippet: Optional[str] = None


class AISearchResponse(BaseModel):
    """Respons akhir endpoint POST /api/v1/search/ai."""

    query: str
    model_used: str
    explanation: str
    parsed_filters: AISearchQueryPlan
    parcel_ids: List[UUID] = Field(default_factory=list)
    total_matched: int
    matches: List[AISearchParcelMatch] = Field(default_factory=list)
    orphaned_matches: List[AISearchOrphanedMatch] = Field(default_factory=list)
