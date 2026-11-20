"""Pydantic schemas untuk autentikasi login, profil user, dan RBAC."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import UserRole


class LoginRequest(BaseModel):
    """Payload login menggunakan email dan password."""

    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=255)


class UserRead(BaseModel):
    """Representasi publik profil pengguna Web GIS LDKH."""

    id: UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    """Respons sukses login berisi JWT access token dan data profil user."""

    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user: UserRead
