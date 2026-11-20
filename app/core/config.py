"""Konfigurasi aplikasi FastAPI berbasis Pydantic Settings."""

import json
from typing import Annotated, List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Pengaturan environment untuk layanan Backend GIS LDKH."""

    PROJECT_NAME: str = "LDKH Web GIS API"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    DATABASE_URL: str = (
        "postgresql+psycopg2://ldkh_admin:ldkh_dev_secret@localhost:5432/ldkh_gis"
    )

    SYNOLOGY_MOUNT_PATH: str = "/mnt/synology/LDKH-Documents"
    UPLOAD_STORAGE_PATH: str = "./storage/uploads"
    NGINX_INTERNAL_PDF_PREFIX: str = "/internal-pdf"

    SECRET_KEY: str = "change-this-in-production-hmac-secret"
    PDF_TOKEN_EXPIRE_SECONDS: int = 900
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720

    # Cloudflare Zero Trust Access (opsional jika tunnel dilindungi Cloudflare Access)
    CF_ACCESS_TEAM_DOMAIN: Optional[str] = None
    CF_ACCESS_AUD: Optional[str] = None

    # Isolasi port backend: wajib lewat Nginx/Frontend internal di produksi
    ENFORCE_REVERSE_PROXY: bool = False
    TRUSTED_PROXY_IPS: Annotated[List[str], NoDecode] = [
        "127.0.0.1",
        "::1",
        "172.16.10.133",
        "172.16.10.131",
    ]

    # Google Gemini API (AI-Powered OCR Search) - wajib diisi via environment variable
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-4-hydrogen"

    CORS_ORIGINS: Annotated[List[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:8080",
    ]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("CORS_ORIGINS", "TRUSTED_PROXY_IPS", mode="before")
    @classmethod
    def parse_str_list(cls, value: str | List[str]) -> List[str]:
        """Mengonversi string dipisahkan koma atau JSON array dari .env menjadi list."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value


settings = Settings()
