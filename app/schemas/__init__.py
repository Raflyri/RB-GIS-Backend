"""Ekspor seluruh Pydantic schemas untuk router Backend API LDKH."""

from app.schemas.auth import LoginRequest, TokenResponse, UserRead
from app.schemas.document import (
    DocumentBase,
    DocumentCreate,
    DocumentDetailRead,
    DocumentRead,
    DocumentUpdate,
    PdfSignedUrlResponse,
)
from app.schemas.ocr import (
    OcrHitlVerifyRequest,
    OcrRawDataBase,
    OcrRawDataCreate,
    OcrRawDataRead,
)
from app.schemas.owner import OwnerBase, OwnerCreate, OwnerRead, OwnerUpdate
from app.schemas.parcel import (
    DuplicateStrategy,
    GeoJSONGeometry,
    ParcelBase,
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
from app.schemas.search import (
    AISearchOrphanedMatch,
    AISearchParcelMatch,
    AISearchQueryPlan,
    AISearchRequest,
    AISearchResponse,
)

__all__ = [
    "AISearchOrphanedMatch",
    "AISearchParcelMatch",
    "AISearchQueryPlan",
    "AISearchRequest",
    "AISearchResponse",
    "DocumentBase",
    "DocumentCreate",
    "DocumentDetailRead",
    "DocumentRead",
    "DocumentUpdate",
    "DuplicateStrategy",
    "GeoJSONGeometry",
    "LoginRequest",
    "OcrHitlVerifyRequest",
    "TokenResponse",
    "UserRead",
    "OcrRawDataBase",
    "OcrRawDataCreate",
    "OcrRawDataRead",
    "OwnerBase",
    "OwnerCreate",
    "OwnerRead",
    "OwnerUpdate",
    "ParcelBase",
    "ParcelCreate",
    "ParcelDetailRead",
    "ParcelGeoJSONFeature",
    "ParcelGeoJSONFeatureCollection",
    "ParcelGeoJSONProperties",
    "ParcelImportFeaturePreview",
    "ParcelImportResponse",
    "ParcelRead",
    "ParcelUpdate",
    "PdfSignedUrlResponse",
    "PlannedImportAction",
]
