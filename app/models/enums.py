"""Enumerasi domain hukum pertanahan, dokumen legal, dan verifikasi OCR LDKH."""

from enum import Enum


class LegalStatus(str, Enum):
    """Status hukum tanah untuk klasifikasi & pewarnaan tematik peta MapLibre."""

    CLEAR = "CLEAR"
    SENGKETA = "SENGKETA"
    BEDA_FISIK = "BEDA_FISIK"
    DISITA_BI = "DISITA_BI"
    RAMPASAN = "RAMPASAN"


class DocumentType(str, Enum):
    """Jenis dokumen legal pertanahan hasil scan / arsip Synology NAS."""

    SHM = "SHM"
    SHGB = "SHGB"
    AJB = "AJB"
    GIRIK = "GIRIK"
    LETTER_C = "LETTER_C"
    SPH = "SPH"
    PUTUSAN_PENGADILAN = "PUTUSAN_PENGADILAN"
    BERITA_ACARA_SITA = "BERITA_ACARA_SITA"
    LAINNYA = "LAINNYA"


class VerificationStatus(str, Enum):
    """Status alur validasi Human-in-the-Loop hasil ekstraksi OCR Claude AI."""

    DRAFT_AI = "DRAFT_AI"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    VERIFIED = "VERIFIED"


class OwnerType(str, Enum):
    """Kategori subjek hukum pemilik / pihak terkait dalam riwayat tanah."""

    INDIVIDU = "INDIVIDU"
    KORPORASI = "KORPORASI"
    INSTANSI_PEMERINTAH = "INSTANSI_PEMERINTAH"
    BANK_INDONESIA = "BANK_INDONESIA"
    AHLI_WARIS = "AHLI_WARIS"


class UserRole(str, Enum):
    """Peran otorisasi (RBAC) pengguna Sistem Web GIS LDKH."""

    ADMIN = "ADMIN"
    AUDITOR = "AUDITOR"
    VIEWER = "VIEWER"
