"""User model."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.company import Company, CompanyUser


class User(BaseModel):
    """Platform user — admin, wholesale company buyer, or retail customer."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50))

    # 'wholesale' | 'retail'
    account_type: Mapped[str] = mapped_column(String(20), default="wholesale", nullable=False)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verification_token: Mapped[str | None] = mapped_column(String(255))

    # Retail account activation (set when retail user is auto-created at guest checkout)
    activation_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    activation_token_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    password_reset_token: Mapped[str | None] = mapped_column(String(255))
    password_reset_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Relationships ─────────────────────────────────────────────────────────
    company_memberships: Mapped[list["CompanyUser"]] = relationship(
        "CompanyUser",
        back_populates="user",
        foreign_keys="[CompanyUser.user_id]",
        cascade="all, delete-orphan",
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"
