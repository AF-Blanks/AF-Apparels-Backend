# backend/app/core/config.py
"""Application configuration loaded from environment variables via Pydantic Settings."""
from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, EmailStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    APP_ENV: Literal["development", "test", "staging", "production"] = "development"
    APP_SECRET_KEY: str = "dev-secret-key-change-in-production"
    DEBUG: bool = False
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:3001,https://af-apparel.vercel.app,https://af-apparel-sigma.vercel.app"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]

    # ── Cookie ────────────────────────────────────────────────────────────────
    COOKIE_SECURE: bool = False       # Set True in production (HTTPS required for SameSite=none)
    COOKIE_DOMAIN: str | None = None  # Leave empty/unset for Railway; omits Domain attribute
    COOKIE_SAMESITE: str = "lax"      # "none" for cross-domain (Railway backend + Vercel frontend)

    @field_validator("COOKIE_DOMAIN", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: object) -> object:
        """Coerce empty string to None so set_cookie omits the Domain attribute."""
        if v == "":
            return None
        return v

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str  # asyncpg URL
    DATABASE_URL_SYNC: str = ""  # psycopg2 URL — auto-derived from DATABASE_URL if not set

    @property
    def sync_db_url(self) -> str:
        """Synchronous DB URL for Alembic — auto-derived from async URL if not set."""
        if self.DATABASE_URL_SYNC:
            return self.DATABASE_URL_SYNC
        return (
            self.DATABASE_URL
            .replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgres+asyncpg://", "postgresql://")
        )

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── JWT ───────────────────────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "dev-jwt-secret-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # ── Stripe ────────────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    # ── QuickBooks ────────────────────────────────────────────────────────────
    QB_CLIENT_ID: str = ""
    QB_CLIENT_SECRET: str = ""
    QB_REDIRECT_URI: str = ""
    QB_ENVIRONMENT: Literal["sandbox", "production"] = "sandbox"
    QB_COMPANY_ID: str = ""
    QB_ACCESS_TOKEN: str = ""
    QB_REFRESH_TOKEN: str = ""

    # ── Email (Resend) ────────────────────────────────────────────────────────
    RESEND_API_KEY: str = ""
    SENDGRID_API_KEY: str = ""  # kept for backward compat, unused
    RESEND_FROM_EMAIL: str = ""
    EMAIL_FROM_ADDRESS: str = "noreply@karauxbaia.resend.app"
    EMAIL_FROM_NAME: str = "AF Apparels"
    ADMIN_NOTIFICATION_EMAIL: str = ""

    @model_validator(mode="after")
    def _apply_resend_from_email(self) -> "Settings":
        if self.RESEND_FROM_EMAIL:
            self.EMAIL_FROM_ADDRESS = self.RESEND_FROM_EMAIL
        return self
    FRONTEND_URL: str = "http://localhost:3000"

    # ── AWS S3 ────────────────────────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = "afapparel-media"
    AWS_S3_REGION: str = "us-east-1"
    CDN_BASE_URL: str = ""

    # ── Shopify (migration only) ──────────────────────────────────────────────
    SHOPIFY_STORE_DOMAIN: str = ""
    SHOPIFY_ADMIN_API_TOKEN: str = ""

    # ── reCAPTCHA ─────────────────────────────────────────────────────────────
    RECAPTCHA_SECRET_KEY: str = ""

    # ── Sentry ────────────────────────────────────────────────────────────────
    SENTRY_DSN: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
