"""Endpoint autentikasi login, logout, dan profil user (JWT + Cloudflare Access)."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import (
    ACCESS_COOKIE_NAME,
    create_access_token,
    get_current_user,
    verify_password,
)
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse, UserRead

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Mengautentikasi pengguna dengan email & password serta menerbitkan sesi JWT."""
    email_normalized = payload.email.strip().lower()
    user = db.query(User).filter(func.lower(User.email) == email_normalized).first()

    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email atau password salah.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akun pengguna ini sedang dinonaktifkan.",
        )

    access_token, expires_in = create_access_token(user)
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=access_token,
        httponly=True,
        samesite="lax",
        max_age=expires_in,
        path="/",
    )

    return TokenResponse(
        access_token=access_token,
        expires_in_seconds=expires_in,
        user=UserRead.model_validate(user),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> Response:
    """Menghapus cookie sesi login dari browser."""
    response.delete_cookie(key=ACCESS_COOKIE_NAME, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead)
def get_me(current_user: User = Depends(get_current_user)) -> User:
    """Mengembalikan profil dan role user yang sedang terautentikasi."""
    return current_user
