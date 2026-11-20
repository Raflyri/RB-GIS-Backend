"""Endpoint REST API untuk tabel tabular `documents`, upload/replace PDF, & orphaned records."""

from decimal import Decimal
from pathlib import Path
import re
from typing import List, Optional
from uuid import UUID, uuid4

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
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.pdf_security import (
    create_pdf_stream_token,
    get_upload_storage_dir,
    sanitize_relative_pdf_path,
    save_uploaded_pdf,
)
from app.core.security import require_admin, require_editor, require_viewer
from app.db.session import get_db
from app.models import (
    Document,
    DocumentType,
    LegalStatus,
    OcrRawData,
    Parcel,
    User,
    VerificationStatus,
)
from app.schemas import (
    DocumentCreate,
    DocumentDetailRead,
    DocumentRead,
    DocumentUpdate,
    PdfSignedUrlResponse,
)
from app.utils.area_normalizer import normalize_to_m2

router = APIRouter()


def _safe_pdf_filename(raw_filename: Optional[str], fallback_stem: str) -> str:
    """Membersihkan nama file PDF unggahan agar aman dan selalu berakhiran .pdf."""
    stem = Path(raw_filename or fallback_stem).stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    if not cleaned:
        cleaned = fallback_stem
    return f"{cleaned[:80]}.pdf"


def _build_upload_relative_path(
    parcel: Optional[Parcel],
    doc_id: UUID,
    filename: str,
) -> str:
    """Membentuk path relatif terstruktur untuk file PDF unggahan di storage backend."""
    if parcel is not None:
        safe_code = re.sub(r"[^A-Za-z0-9._-]+", "-", parcel.parcel_code).strip("-") or str(parcel.id)
        return f"Uploads/Parcels/{safe_code}/{doc_id}_{filename}"
    return f"Uploads/Orphaned/{doc_id}_{filename}"


@router.get("/", response_model=List[DocumentRead])
def list_documents(
    legal_status: Optional[LegalStatus] = Query(default=None),
    verification_status: Optional[VerificationStatus] = Query(default=None),
    parcel_id: Optional[UUID] = Query(default=None),
    orphaned_only: bool = Query(
        default=False,
        description="Jika True, hanya menampilkan dokumen yang belum tertaut ke poligon persil (parcel_id IS NULL).",
    ),
    search: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> List[Document]:
    """Mengambil daftar dokumen legal termasuk filter orphaned records."""
    query = db.query(Document)

    if legal_status is not None:
        query = query.filter(Document.legal_status == legal_status)
    if verification_status is not None:
        query = query.filter(Document.verification_status == verification_status)
    if orphaned_only:
        query = query.filter(Document.parcel_id.is_(None))
    elif parcel_id is not None:
        query = query.filter(Document.parcel_id == parcel_id)

    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Document.document_number.ilike(term),
                Document.title.ilike(term),
                Document.issuing_office.ilike(term),
            )
        )

    return query.order_by(Document.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/orphaned", response_model=List[DocumentRead])
def list_orphaned_documents(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> List[Document]:
    """Mengambil daftar dokumen orphaned yang belum memiliki poligon fisik di PostGIS."""
    return (
        db.query(Document)
        .filter(Document.parcel_id.is_(None))
        .order_by(Document.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{document_id}", response_model=DocumentDetailRead)
def get_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> Document:
    """Mengambil detail dokumen lengkap beserta daftar pemilik dan metadata OCR."""
    document = (
        db.query(Document)
        .options(selectinload(Document.owners), selectinload(Document.ocr_raw_data))
        .filter(Document.id == document_id)
        .first()
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )
    return document


@router.post("/", response_model=DocumentDetailRead, status_code=status.HTTP_201_CREATED)
def create_document(
    payload: DocumentCreate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_editor),
) -> Document:
    """Menyimpan metadata dokumen legal (tanpa BLOB PDF) dengan normalisasi luasan ke m2."""
    if payload.parcel_id is not None:
        parcel_exists = (
            db.query(Parcel.id).filter(Parcel.id == payload.parcel_id).first()
        )
        if parcel_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Persil target dengan ID {payload.parcel_id} tidak ditemukan.",
            )

    try:
        clean_path = sanitize_relative_pdf_path(payload.synology_file_path)
        stated_area_m2 = normalize_to_m2(
            payload.original_area_value, payload.original_area_unit
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    doc_data = payload.model_dump(exclude={"initial_ocr"})
    doc_data["synology_file_path"] = clean_path
    doc_data["stated_area_m2"] = stated_area_m2

    document = Document(**doc_data)
    db.add(document)
    db.flush()

    if payload.initial_ocr is not None:
        ocr_record = OcrRawData(
            document_id=document.id,
            **payload.initial_ocr.model_dump(),
        )
        db.add(ocr_record)

    db.commit()
    return get_document(document.id, db)


@router.post("/upload", response_model=DocumentDetailRead, status_code=status.HTTP_201_CREATED)
async def upload_document_pdf(
    file: UploadFile = File(...),
    parcel_id: Optional[UUID] = Form(default=None),
    document_number: Optional[str] = Form(default=None),
    document_type: DocumentType = Form(default=DocumentType.LAINNYA),
    title: Optional[str] = Form(default=None),
    issuing_office: Optional[str] = Form(default=None),
    legal_status: LegalStatus = Form(default=LegalStatus.CLEAR),
    original_area_value: Optional[Decimal] = Form(default=None),
    original_area_unit: Optional[str] = Form(default="m2"),
    notes: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
) -> Document:
    """Mengunggah file PDF baru ke storage backend dan mendaftarkan metadata dokumen (mendukung 1-to-Many)."""
    parcel: Optional[Parcel] = None
    if parcel_id is not None:
        parcel = db.query(Parcel).filter(Parcel.id == parcel_id).first()
        if parcel is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Persil target dengan ID {parcel_id} tidak ditemukan.",
            )

    content = await file.read()
    doc_id = uuid4()
    safe_name = _safe_pdf_filename(file.filename, fallback_stem=f"doc-{doc_id.hex[:8]}")
    relative_path = _build_upload_relative_path(parcel, doc_id, safe_name)

    try:
        saved_path, checksum = save_uploaded_pdf(content, relative_path)
        stated_area_m2 = normalize_to_m2(original_area_value, original_area_unit)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    resolved_number = (
        document_number.strip()
        if document_number and document_number.strip()
        else f"DOC-{doc_id.hex[:8].upper()}"
    )[:128]
    resolved_title = (
        title.strip()
        if title and title.strip()
        else Path(file.filename or safe_name).stem
    )[:255]

    document = Document(
        id=doc_id,
        parcel_id=parcel_id,
        document_number=resolved_number,
        document_type=document_type,
        title=resolved_title,
        issuing_office=issuing_office.strip()[:255] if issuing_office and issuing_office.strip() else None,
        legal_status=legal_status,
        stated_area_m2=stated_area_m2,
        original_area_value=original_area_value,
        original_area_unit=original_area_unit or "m2",
        synology_file_path=saved_path,
        file_checksum_sha256=checksum,
        verification_status=VerificationStatus.NEEDS_REVIEW,
    )
    db.add(document)
    db.flush()

    ocr_record = OcrRawData(
        document_id=document.id,
        ocr_engine="manual-pdf-upload",
        raw_text=f"Dokumen {resolved_number} ({resolved_title}) diunggah oleh {current_user.email}. {notes or ''}".strip(),
        extracted_json={
            "document_number": resolved_number,
            "document_type": document_type.value,
            "title": resolved_title,
            "uploaded_filename": file.filename or safe_name,
            "uploaded_by": current_user.email,
            "notes": notes,
        },
        verification_status=VerificationStatus.NEEDS_REVIEW,
        review_notes=notes.strip() if notes and notes.strip() else f"PDF diunggah oleh {current_user.full_name} ({current_user.email}).",
    )
    db.add(ocr_record)
    db.commit()
    return get_document(document.id, db)


@router.put("/{document_id}/file", response_model=DocumentDetailRead)
@router.post("/{document_id}/file", response_model=DocumentDetailRead, include_in_schema=False)
async def replace_document_pdf(
    document_id: UUID,
    file: UploadFile = File(...),
    title: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
) -> Document:
    """Mengunggah atau mengganti file PDF fisik untuk dokumen eksisting (termasuk dummy CSV)."""
    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )

    parcel: Optional[Parcel] = None
    if document.parcel_id is not None:
        parcel = db.query(Parcel).filter(Parcel.id == document.parcel_id).first()

    old_path = document.synology_file_path
    content = await file.read()
    safe_name = _safe_pdf_filename(file.filename, fallback_stem=f"doc-{document.id.hex[:8]}")
    relative_path = _build_upload_relative_path(parcel, document.id, safe_name)

    try:
        saved_path, checksum = save_uploaded_pdf(content, relative_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # Bersihkan file lama apabila sebelumnya berada di storage upload backend dengan nama berbeda
    if old_path and old_path != saved_path and old_path.startswith("Uploads/"):
        upload_base = get_upload_storage_dir()
        old_target = (upload_base / old_path).resolve()
        if old_target.is_relative_to(upload_base) and old_target.exists() and old_target.is_file():
            try:
                old_target.unlink()
            except OSError:
                pass

    document.synology_file_path = saved_path
    document.file_checksum_sha256 = checksum
    if title and title.strip():
        document.title = title.strip()[:255]

    ocr = db.query(OcrRawData).filter(OcrRawData.document_id == document.id).first()
    if ocr is not None:
        note_entry = f"File PDF diganti/diunggah ({file.filename or safe_name}) oleh {current_user.email}."
        ocr.review_notes = f"{ocr.review_notes} | {note_entry}" if ocr.review_notes else note_entry

    db.commit()
    return get_document(document.id, db)


@router.patch("/{document_id}", response_model=DocumentDetailRead)
def update_document(
    document_id: UUID,
    payload: DocumentUpdate,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_editor),
) -> Document:
    """Memperbarui metadata dokumen atau menautkan orphaned record ke parcel_id."""
    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )

    update_data = payload.model_dump(exclude_unset=True)

    if "parcel_id" in update_data and update_data["parcel_id"] is not None:
        parcel_exists = (
            db.query(Parcel.id).filter(Parcel.id == update_data["parcel_id"]).first()
        )
        if parcel_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Persil target dengan ID {update_data['parcel_id']} tidak ditemukan.",
            )

    if "synology_file_path" in update_data and update_data["synology_file_path"]:
        try:
            update_data["synology_file_path"] = sanitize_relative_pdf_path(
                update_data["synology_file_path"]
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    for field, value in update_data.items():
        setattr(document, field, value)

    if "original_area_value" in update_data or "original_area_unit" in update_data:
        try:
            document.stated_area_m2 = normalize_to_m2(
                document.original_area_value, document.original_area_unit
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    db.commit()
    return get_document(document.id, db)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_admin),
) -> Response:
    """Menghapus metadata dokumen dari database (hanya role ADMIN; file fisik di Synology tetap aman)."""
    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )

    db.delete(document)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{document_id}/pdf-url", response_model=PdfSignedUrlResponse)
def get_signed_pdf_url(
    document_id: UUID,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_viewer),
) -> PdfSignedUrlResponse:
    """Men-generate tokenized URL sementara untuk melihat dokumen PDF di Split-pane Viewer."""
    document = db.query(Document).filter(Document.id == document_id).first()
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dokumen dengan ID {document_id} tidak ditemukan.",
        )

    try:
        token, expires_in = create_pdf_stream_token(
            document.id, document.synology_file_path
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    stream_url = f"{settings.API_V1_STR}/files/stream/{document.id}?token={token}"
    return PdfSignedUrlResponse(
        document_id=document.id,
        document_number=document.document_number,
        stream_url=stream_url,
        expires_in_seconds=expires_in,
        content_disposition="inline",
    )
