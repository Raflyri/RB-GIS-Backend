"""Pydantic schemas untuk autentikasi login, profil user, Guest JIT Access, dan RBAC."""

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import GuestRequestStatus, UserRole


class LoginRequest(BaseModel):
    """Payload login menggunakan email dan password."""

    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)


class UserRead(BaseModel):
    """Representasi publik profil pengguna Web GIS LDKH (termasuk sesi Guest read-only)."""

    id: UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    short_code: Optional[str] = None
    session_expires_at: Optional[datetime] = None
    ip_address: Optional[str] = None
    device_summary: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    """Respons sukses login / guest exchange berisi JWT access token dan data profil user."""

    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user: UserRead


class GuestAccessRequestPayload(BaseModel):
    """Payload opsional dari guest saat mengajukan permintaan akses sementara."""

    guest_name: Optional[str] = Field(default=None, max_length=100)


class GuestAccessRequestResponse(BaseModel):
    """Respons saat guest mengajukan permintaan akses dan masuk ke Waiting Room."""

    short_code: str
    wait_token: str
    status: GuestRequestStatus
    request_expires_at: datetime
    requested_at_wib: str
    device_summary: str
    ip_address: str


class GuestAccessStatusResponse(BaseModel):
    """Respons polling status permintaan akses guest dari Waiting Room."""

    status: GuestRequestStatus
    short_code: str
    request_expires_at: datetime
    decided_at: Optional[datetime] = None


class GuestExchangeRequest(BaseModel):
    """Payload penukaran wait_token yang sudah disetujui menjadi sesi Guest 3 jam."""

    wait_token: str = Field(..., min_length=16, max_length=255)


class GuestApprovalDetailResponse(BaseModel):
    """Detail permintaan akses guest yang ditampilkan pada Unique Approval URL HP Owner."""

    short_code: str
    guest_name: Optional[str] = None
    device_summary: str
    ip_address: str
    requested_at: datetime
    requested_at_wib: str
    request_expires_at: datetime
    remaining_seconds: int
    status: GuestRequestStatus
    decided_at: Optional[datetime] = None
    decided_via: Optional[str] = None
    is_actionable: bool


class GuestApprovalActionPayload(BaseModel):
    """Keputusan Owner melalui Unique Approval URL."""

    action: Literal["APPROVE", "REJECT"]


class GuestApprovalActionResponse(BaseModel):
    """Respons setelah Owner menekan tombol Izinkan atau Tolak pada Unique Approval URL."""

    short_code: str
    status: GuestRequestStatus
    decided_at: datetime
    decided_via: str
    message: str


class HermesPendingRequestItem(BaseModel):
    """Item permintaan akses guest yang tertunda untuk recovery polling CT 101."""

    request_id: UUID
    short_code: str
    guest_name: Optional[str] = None
    device_summary: str
    ip_address: str
    requested_at: datetime
    requested_at_wib: str
    request_expires_at: datetime
    remaining_seconds: int
    target_url: str
    message: str


class HermesPendingResponse(BaseModel):
    """Daftar permintaan PENDING yang belum ternotifikasi atau masih aktif."""

    count: int
    items: List[HermesPendingRequestItem]


class HermesDecisionPayload(BaseModel):
    """Payload keputusan Owner melalui WhatsApp Hermes AI Agent."""

    action: Literal["APPROVE", "REJECT", "REVOKE"]
    short_code: Optional[str] = Field(default=None, max_length=16)


class HermesDecisionResponse(BaseModel):
    """Respons hasil eksekusi keputusan Hermes."""

    short_code: str
    status: GuestRequestStatus
    action: str
    decided_at: datetime
    decided_via: str
    device_summary: str
    ip_address: str
    message: str
