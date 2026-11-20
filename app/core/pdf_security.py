"""Utilitas keamanan token JWT dan resolusi path dokumen PDF Synology/Nextcloud."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import jwt

from app.core.config import settings

ALGORITHM = "HS256"
MAX_PDF_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


def sanitize_relative_pdf_path(relative_path: str) -> str:
    """Validasi dan normalisasi path relatif dokumen agar aman dari path traversal."""
    if not relative_path or "\x00" in relative_path:
        raise ValueError("Path dokumen tidak valid atau kosong.")

    normalized = relative_path.replace("\\", "/").strip()

    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ValueError("Path dokumen harus berupa path relatif di dalam brankas Synology.")

    posix_path = PurePosixPath(normalized)
    clean_parts: list[str] = []
    for part in posix_path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("Path dokumen mengandung segmen traversal ('..') yang dilarang.")
        clean_parts.append(part)

    if not clean_parts:
        raise ValueError("Path dokumen tidak boleh kosong.")

    return "/".join(clean_parts)


def create_pdf_stream_token(document_id: UUID, relative_path: str) -> tuple[str, int]:
    """Men-generate JWT short-lived token untuk streaming PDF inline secara aman."""
    sanitized_path = sanitize_relative_pdf_path(relative_path)
    now = datetime.now(timezone.utc)
    expires_in = settings.PDF_TOKEN_EXPIRE_SECONDS
    payload: dict[str, Any] = {
        "sub": str(document_id),
        "path": sanitized_path,
        "scope": "pdf:stream",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)
    return token, expires_in


def verify_pdf_stream_token(token: str, expected_document_id: UUID) -> dict[str, Any]:
    """Memverifikasi JWT token streaming PDF dan kecocokan document_id."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise ValueError("Token akses PDF telah kedaluwarsa.") from exc
    except jwt.InvalidTokenError as exc:
        raise ValueError("Token akses PDF tidak valid.") from exc

    if payload.get("scope") != "pdf:stream":
        raise ValueError("Scope token akses PDF tidak sesuai.")

    if payload.get("sub") != str(expected_document_id):
        raise ValueError("Token tidak cocok dengan dokumen yang diminta.")

    return payload


def get_upload_storage_dir() -> Path:
    """Mengembalikan direktori penyimpanan lokal backend untuk file PDF yang diunggah via Web GIS."""
    configured = Path(settings.UPLOAD_STORAGE_PATH)
    if not configured.is_absolute():
        backend_root = Path(__file__).resolve().parents[2]
        configured = backend_root / configured
    return configured.resolve()


def get_synology_base_dir() -> Path:
    """Mengembalikan direktori dasar dokumen Synology (atau mock lokal saat development)."""
    configured = Path(settings.SYNOLOGY_MOUNT_PATH)
    if configured.exists() and configured.is_dir():
        return configured.resolve()

    repo_root = Path(__file__).resolve().parents[3]
    mock_dir = repo_root / "infra" / "mock-synology" / "LDKH-Documents"
    return mock_dir.resolve()


def save_uploaded_pdf(content: bytes, relative_path: str) -> tuple[str, str]:
    """Memvalidasi biner PDF, menyimpannya ke storage upload backend, dan mengembalikan (path, sha256)."""
    if not content:
        raise ValueError("File PDF kosong.")
    if len(content) > MAX_PDF_UPLOAD_BYTES:
        raise ValueError("Ukuran file PDF melebihi batas maksimal 50 MB.")
    if not content.lstrip().startswith(b"%PDF-"):
        raise ValueError("File tidak valid: wajib berformat PDF (%PDF-).")

    sanitized = sanitize_relative_pdf_path(relative_path)
    upload_base = get_upload_storage_dir()
    upload_base.mkdir(parents=True, exist_ok=True)
    target = (upload_base / sanitized).resolve()

    if not target.is_relative_to(upload_base):
        raise ValueError("Akses path di luar direktori upload ditolak.")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    checksum = hashlib.sha256(content).hexdigest()
    return sanitized, checksum


def resolve_synology_file_path(relative_path: str) -> Path:
    """Mencari file PDF secara aman di upload storage backend, mount Synology, atau mock lokal."""
    sanitized = sanitize_relative_pdf_path(relative_path)

    upload_base = get_upload_storage_dir()
    upload_target = (upload_base / sanitized).resolve()
    if not upload_target.is_relative_to(upload_base):
        raise ValueError("Akses path di luar direktori upload ditolak.")
    if upload_target.exists() and upload_target.is_file():
        return upload_target

    base_dir = get_synology_base_dir()
    target = (base_dir / sanitized).resolve()
    if not target.is_relative_to(base_dir):
        raise ValueError("Akses path di luar direktori mount Synology ditolak.")

    return target


def _escape_pdf_text(text: str) -> str:
    """Escape karakter khusus pada string literal PDF."""
    return (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .encode("ascii", errors="replace")
        .decode("ascii")
    )


def generate_sample_pdf_bytes(title: str, doc_number: str, synology_path: str) -> bytes:
    """Menghasilkan file PDF %PDF-1.4 valid secara dinamis dengan xref offset akurat."""
    lines = [
        "SISTEM WEB GIS DIGITALISASI TANAH LDKH",
        "Arsip Dokumen Legal (Synology DS218j / Nextcloud Linker)",
        f"Nomor Dokumen : {doc_number}",
        f"Judul Dokumen : {title}",
        f"Path Target   : {synology_path}",
        "Status File   : Belum ada PDF fisik (dummy CSV / placeholder).",
        "Tindakan      : Gunakan tombol 'Ganti / Upload PDF' untuk mengunggah scan asli.",
    ]

    stream_commands = ["BT", "/F1 12 Tf", "50 750 Td", "18 TL"]
    for idx, line in enumerate(lines):
        escaped = _escape_pdf_text(line)
        if idx == 0:
            stream_commands.append(f"({escaped}) Tj")
        else:
            stream_commands.append(f"T* ({escaped}) Tj")
    stream_commands.append("ET")
    content_stream = "\n".join(stream_commands).encode("ascii")

    objects: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n"
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\n"
            b"endobj\n"
        ),
        (
            f"4 0 obj\n<< /Length {len(content_stream)} >>\nstream\n".encode("ascii")
            + content_stream
            + b"\nendstream\nendobj\n"
        ),
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]

    header = b"%PDF-1.4\n"
    body = bytearray(header)
    offsets: list[int] = [0]

    for obj in objects:
        offsets.append(len(body))
        body.extend(obj)

    xref_start = len(body)
    xref_lines = [f"xref\n0 {len(offsets)}\n".encode("ascii")]
    xref_lines.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        xref_lines.append(f"{offset:010d} 00000 n \n".encode("ascii"))

    trailer = (
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_start}\n%%EOF\n"
    ).encode("ascii")

    body.extend(b"".join(xref_lines))
    body.extend(trailer)
    return bytes(body)
