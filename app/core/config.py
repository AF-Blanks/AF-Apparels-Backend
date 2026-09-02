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
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:3001,https://af-apparel.vercel.app,https://af-apparel-sigma.vercel.app,https://af-apparels.vercel.app,https://af-apparels-sigma.vercel.app"

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

    @model_validator(mode="after")
    def _cookie_defaults_for_prod(self) -> "Settings":
        """Production/staging run cross-domain (afblanks.com / Vercel frontend +
        Railway backend). A SameSite=lax refresh cookie is NOT sent on the
        cross-site POST to /api/v1/refresh, so the silent token refresh fails and
        users get logged out the moment their access token expires (feels like
        "logged out on any inactivity"). Force SameSite=None + Secure there so the
        refresh cookie is delivered and the session renews seamlessly. Local dev
        (development/test over http) keeps lax/insecure so the cookie still works.
        """
        if self.APP_ENV in ("production", "staging"):
            self.COOKIE_SAMESITE = "none"
            self.COOKIE_SECURE = True
        return self

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
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 30  # 30 days — long-lived so a
    # temporary refresh hiccup never logs the user out mid-session
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    @model_validator(mode="after")
    def _force_long_session_in_prod(self) -> "Settings":
        """Users were being logged out after only a little inactivity. Root cause:
        a SHORT access-token expiry left in the Railway env (from initial setup)
        overrides the intended 30-day default, so the token expires quickly and any
        refresh hiccup drops the session. In production/staging, never let the
        access token live less than 30 days (nor the refresh token under 30 days),
        regardless of what the env var says — so a brief idle never logs anyone out.
        Note: only affects NEWLY issued tokens, so users must sign in once more to
        pick up the long-lived token."""
        if self.APP_ENV in ("production", "staging"):
            THIRTY_DAYS_MIN = 60 * 24 * 30
            if self.JWT_ACCESS_TOKEN_EXPIRE_MINUTES < THIRTY_DAYS_MIN:
                self.JWT_ACCESS_TOKEN_EXPIRE_MINUTES = THIRTY_DAYS_MIN
            if self.JWT_REFRESH_TOKEN_EXPIRE_DAYS < 30:
                self.JWT_REFRESH_TOKEN_EXPIRE_DAYS = 30
        return self

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
    QB_SHIPPING_ITEM_ID: str = ""       # QB item ID for Shipping & Handling line
    QB_CONVENIENCE_FEE_ITEM_ID: str = ""  # QB item ID for 3% Credit Card Convenience Fee
    # QB item ID for the "Sales Tax Collected" service item, mapped to the
    # Sales Tax Payable *liability* account (id 30). Tax charged at checkout
    # (ZipTax/manual) is posted here as an explicit line so QB's Automated Sales
    # Tax never recalculates and the invoice total always equals the customer's
    # charge. Default is this company's live item; override via env if it changes.
    QB_TAX_ITEM_ID: str = "584"
    # Every sold line is billed against this one item. QuickBooks will not take
    # an invoice line without an item, but it does not need one per product to
    # keep books — the product is named in the line description, and the
    # catalogue stays here, where it is managed.
    QB_MERCHANDISE_ITEM_ID: str = ""

    # What QuickBooks is used for. Invoices and payments, and nothing about
    # stock: a catalogue in two places has to be kept in step, and an inventory
    # ledger in two places will disagree the first time either is touched
    # outside the other.
    QB_SYNC_CATALOG: bool = False      # per-variant items
    QB_SYNC_INVENTORY: bool = False    # quantity on hand
    # A receipt from a supplier creates a bill — money owed — so it belongs in
    # the books whatever happens to the catalogue. Without a per-variant item
    # the bill lines fall to the Cost of Goods Sold account with the product
    # named on them, which is where they belong anyway.
    QB_SYNC_PURCHASES: bool = True     # supplier bills from PO receipts

    # Carriers we wait for when quoting rates. Shippo fills a shipment's rates in
    # progressively, so a carrier that is slow to answer is simply absent from the
    # first response — we re-poll until these have arrived. Only list carriers
    # actually connected in Shippo: waiting on one that is not connected burns the
    # whole retry budget on every single lookup and never succeeds. Add "fedex"
    # here once that account is connected.
    SHIPPO_EXPECTED_CARRIERS: str = "ups,usps"

    # ── Email (Resend) ────────────────────────────────────────────────────────
    RESEND_API_KEY: str = ""
    SENDGRID_API_KEY: str = ""  # kept for backward compat, unused
    RESEND_FROM_EMAIL: str = ""
    # Send from the verified afblanks.com domain so mail reaches every customer.
    # (The old test domain only allowed sending to the account owner → 403 for
    # everyone else.) RESEND_FROM_EMAIL still overrides this if set in the env.
    EMAIL_FROM_ADDRESS: str = "noreply@afblanks.com"
    EMAIL_FROM_NAME: str = "AF Apparels"
    ADMIN_NOTIFICATION_EMAIL: str = ""
    # Business inboxes that receive a copy of every new-order alert
    # (comma-separated, env-overridable). Customer still gets their own confirmation.
    # Every notification that goes to the business rather than to a customer —
    # a new order, a wholesale application, low stock, backorders now shippable.
    # One list so they cannot drift: an alert arriving in one inbox and not
    # another is how something ends up unattended.
    ORDER_ALERT_EMAILS: str = "invoice@afblanks.com,info@afblanks.com"
    # Every customer-facing email is blind-copied here so the business keeps one
    # record of what customers were actually sent. Blank disables the copy.
    EMAIL_ARCHIVE_BCC: str = "invoice@afblanks.com"

    # Two emails used to follow an order: an "Order Received" confirmation, then
    # the invoice once QuickBooks had raised it. The owner wants the invoice only.
    #
    # Kept as a switch rather than deleted because the invoice email depends on
    # the QuickBooks sync succeeding — if that stalls, the customer hears nothing
    # at all. Set this back to true and the confirmation resumes immediately.
    SEND_ORDER_CONFIRMATION_EMAIL: bool = False

    @model_validator(mode="after")
    def _apply_resend_from_email(self) -> "Settings":
        if self.RESEND_FROM_EMAIL:
            self.EMAIL_FROM_ADDRESS = self.RESEND_FROM_EMAIL
        return self
    FRONTEND_URL: str = "https://afblanks.com"  # live domain — all email links (reset, order, RMA…) use this

    @model_validator(mode="after")
    def _force_live_frontend_url(self) -> "Settings":
        """EVERY email link (reset password, order, invoice, RMA…) is built from
        FRONTEND_URL. If it's ever left pointing at an old Vercel preview domain
        (or is blank/localhost) in production, customers get emails whose links go
        to a *different* domain than the sender — which hurts deliverability and is
        exactly what Resend flagged. Force it to the live domain in prod/staging so
        this can never recur, regardless of what the Railway env var says."""
        url = (self.FRONTEND_URL or "").strip().lower()
        if self.APP_ENV in ("production", "staging"):
            if (not url) or ("vercel.app" in url) or ("localhost" in url) or ("127.0.0.1" in url):
                self.FRONTEND_URL = "https://afblanks.com"
        return self

    # ── AWS S3 ────────────────────────────────────────────────────────────────
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = "afapparel-media"
    AWS_S3_REGION: str = "us-east-1"
    CDN_BASE_URL: str = ""

    # ── Shopify (migration only) ──────────────────────────────────────────────
    SHOPIFY_STORE_DOMAIN: str = ""
    SHOPIFY_ADMIN_API_TOKEN: str = ""

    # ── Brand / notifications ─────────────────────────────────────────────────
    LOGO_URL: str = ""
    LOW_STOCK_THRESHOLD: int = 10

    # ── reCAPTCHA ─────────────────────────────────────────────────────────────
    RECAPTCHA_SECRET_KEY: str = ""

    # ── Sentry ────────────────────────────────────────────────────────────────
    SENTRY_DSN: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
