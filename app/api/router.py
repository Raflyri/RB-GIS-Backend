"""Aggregator router API v1 untuk Sistem Web GIS Digitalisasi Tanah LDKH."""

from fastapi import APIRouter

from app.api.endpoints import auth, documents, files, ocr, owners, parcels, search

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(parcels.router, prefix="/parcels", tags=["parcels"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(owners.router, prefix="/owners", tags=["owners"])
api_router.include_router(ocr.router, prefix="/ocr", tags=["ocr-hitl"])
api_router.include_router(files.router, prefix="/files", tags=["files-synology"])
api_router.include_router(search.router, prefix="/search", tags=["ai-search"])
