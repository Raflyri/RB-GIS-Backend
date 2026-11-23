"""Ekspor seluruh model SQLAlchemy & Enum agar otomatis terdaftar pada Base.metadata."""

from app.db.session import Base
from app.models.document import Document
from app.models.enums import (
    DocumentType,
    GuestRequestStatus,
    LegalStatus,
    OwnerType,
    UserRole,
    VerificationStatus,
)
from app.models.guest import GuestAccessRequest, GuestSession
from app.models.ocr_raw_data import OcrRawData
from app.models.owner import Owner
from app.models.parcel import Parcel
from app.models.user import User

__all__ = [
    "Base",
    "Document",
    "DocumentType",
    "GuestAccessRequest",
    "GuestRequestStatus",
    "GuestSession",
    "LegalStatus",
    "OcrRawData",
    "Owner",
    "OwnerType",
    "Parcel",
    "User",
    "UserRole",
    "VerificationStatus",
]
