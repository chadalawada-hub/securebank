"""
config.py – Centralised application settings via pydantic-settings.
All values are read from environment variables / .env file.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, field_validator
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Application ──────────────────────────────────────────────
    app_name: str = "SecureBank"
    app_env: str = "development"
    debug: bool = False
    secret_key: str

    # ── JWT ──────────────────────────────────────────────────────
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # ── Database ─────────────────────────────────────────────────
    database_url: str
    database_sync_url: str

    # ── AWS KMS ──────────────────────────────────────────────────
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    kms_key_id: str = ""

    # ── OAuth2 ───────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/oauth/google/callback"

    # ── CORS ─────────────────────────────────────────────────────
    allowed_origins: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    # ── Rate Limiting ─────────────────────────────────────────────
    rate_limit_login: str = "5/minute"
    rate_limit_signup: str = "3/minute"


@lru_cache()
def get_settings() -> Settings:
    """Cached singleton – call get_settings() everywhere."""
    return Settings()
