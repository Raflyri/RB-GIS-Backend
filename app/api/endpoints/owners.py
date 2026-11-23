"""Endpoint REST API untuk tabel `owners` (riwayat kepemilikan tanah berantai)."""

from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.security import require_admin, require_viewer
from app.db.session import get_db
from app.models import Document, Owner, OwnerType, Parcel, User
from app.schemas import OwnerCreate, OwnerRead, OwnerUpdate

router = APIRouter()


@router.get("", response_model=List[OwnerRead], include_in_schema=False)
@router.get("/", response_model=List[OwnerRead])
def list_owners(
    parcel_id: Optional[UUID] = Query(default=None),
    document_id: Optional[UUID] = Query(default=None),
    is_current_owner: Optional[bool] = Query(default=None),
    owner_type: Optional[OwnerType] = Query(default=None),
    search: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> List[Owner]:
    """Mengambil daftar riwayat kepemilikan tanah secara berurutan."""
    query = db.query(Owner)

    if parcel_id is not None:
        query = query.filter(Owner.parcel_id == parcel_id)
    if document_id is not None:
        query = query.filter(Owner.document_id == document_id)
    if is_current_owner is not None:
        query = query.filter(Owner.is_current_owner == is_current_owner)
    if owner_type is not None:
        query = query.filter(Owner.owner_type == owner_type)
    if search:
        query = query.filter(Owner.owner_name.ilike(f"%{search.strip()}%"))

    return (
        query.order_by(Owner.sequence_order.asc(), Owner.created_at.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{owner_id}", response_model=OwnerRead)
def get_owner(
    owner_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> Owner:
    """Mengambil detail satu catatan riwayat kepemilikan."""
    owner = db.query(Owner).filter(Owner.id == owner_id).first()
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Catatan kepemilikan dengan ID {owner_id} tidak ditemukan.",
        )
    return owner


@router.post("", response_model=OwnerRead, status_code=status.HTTP_201_CREATED, include_in_schema=False)
@router.post("/", response_model=OwnerRead, status_code=status.HTTP_201_CREATED)
def create_owner(
    payload: OwnerCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> Owner:
    """Menambahkan catatan pemilik baru dalam rantai riwayat kepemilikan."""
    if payload.parcel_id is not None:
        if db.query(Parcel.id).filter(Parcel.id == payload.parcel_id).first() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Persil dengan ID {payload.parcel_id} tidak ditemukan.",
            )

    if payload.document_id is not None:
        if db.query(Document.id).filter(Document.id == payload.document_id).first() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dokumen dengan ID {payload.document_id} tidak ditemukan.",
            )

    if payload.is_current_owner and payload.mark_previous_non_current:
        if payload.parcel_id is not None:
            db.query(Owner).filter(
                Owner.parcel_id == payload.parcel_id,
                Owner.is_current_owner.is_(True),
            ).update({"is_current_owner": False})
        elif payload.document_id is not None:
            db.query(Owner).filter(
                Owner.document_id == payload.document_id,
                Owner.is_current_owner.is_(True),
            ).update({"is_current_owner": False})

    owner_data = payload.model_dump(exclude={"mark_previous_non_current"})
    owner = Owner(**owner_data)
    db.add(owner)
    db.commit()
    db.refresh(owner)
    return owner


@router.patch("/{owner_id}", response_model=OwnerRead)
def update_owner(
    owner_id: UUID,
    payload: OwnerUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> Owner:
    """Memperbarui data riwayat kepemilikan tanah."""
    owner = db.query(Owner).filter(Owner.id == owner_id).first()
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Catatan kepemilikan dengan ID {owner_id} tidak ditemukan.",
        )

    update_data = payload.model_dump(exclude_unset=True)
    if "parcel_id" in update_data and update_data["parcel_id"] is not None:
        if (
            db.query(Parcel.id).filter(Parcel.id == update_data["parcel_id"]).first()
            is None
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Persil dengan ID {update_data['parcel_id']} tidak ditemukan.",
            )
    if "document_id" in update_data and update_data["document_id"] is not None:
        if (
            db.query(Document.id)
            .filter(Document.id == update_data["document_id"])
            .first()
            is None
        ):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dokumen dengan ID {update_data['document_id']} tidak ditemukan.",
            )

    target_parcel_id = update_data.get("parcel_id", owner.parcel_id)
    target_document_id = update_data.get("document_id", owner.document_id)
    if update_data.get("is_current_owner") is True:
        if target_parcel_id is not None:
            db.query(Owner).filter(
                Owner.parcel_id == target_parcel_id,
                Owner.id != owner.id,
                Owner.is_current_owner.is_(True),
            ).update({"is_current_owner": False}, synchronize_session=False)
        elif target_document_id is not None:
            db.query(Owner).filter(
                Owner.document_id == target_document_id,
                Owner.id != owner.id,
                Owner.is_current_owner.is_(True),
            ).update({"is_current_owner": False}, synchronize_session=False)

    for field, value in update_data.items():
        setattr(owner, field, value)

    db.commit()
    db.refresh(owner)
    return owner


@router.delete("/{owner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_owner(
    owner_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> Response:
    """Menghapus catatan riwayat kepemilikan."""
    owner = db.query(Owner).filter(Owner.id == owner_id).first()
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Catatan kepemilikan dengan ID {owner_id} tidak ditemukan.",
        )

    db.delete(owner)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
