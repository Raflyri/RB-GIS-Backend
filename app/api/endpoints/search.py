"""Endpoint REST API untuk pencarian dokumen & persil berbasis Google Gemini AI."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import require_viewer
from app.db.session import get_db
from app.models import User
from app.schemas.search import AISearchRequest, AISearchResponse
from app.services.ai_search import (
    GeminiNotConfiguredError,
    GeminiUpstreamError,
    run_ai_search,
)

router = APIRouter()


@router.post("/ai", response_model=AISearchResponse)
def search_parcels_with_ai(
    payload: AISearchRequest,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> AISearchResponse:
    """Menerjemahkan prompt pencarian alami via Gemini API lalu mengeksekusinya ke PostGIS."""
    try:
        return run_ai_search(db, payload.query)
    except GeminiNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except GeminiUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
