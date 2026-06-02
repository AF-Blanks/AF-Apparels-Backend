"""System models: Settings, AuditLog, QBSyncLog, WebhookLog, PriceListRequest, AppSettings."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BaseModel

if TYPE_CHECKING:
    from app.models.user import User


class Settings(BaseModel):
    """Platform-wide configuration managed by admin."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))


class AuditLog(BaseModel):
    """Immutable record of admin write operations."""

    __tablename__ = "audit_log"

    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(
        Enum("CREATE", "UPDATE", "DELETE", name="audit_action"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(100), index=True)
    old_values: Mapped[str | None] = mapped_column(Text, comment="JSON of old field values")
    new_values: Mapped[str | None] = mapped_column(Text, comment="JSON of new field values")
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(500))

    admin_user: Mapped["User | None"] = relationship("User")


class QBSyncLog(BaseModel):
    """Tracks every QuickBooks sync attempt."""

    __tablename__ = "qb_sync_log"

    entity_type: Mapped[str] = mapped_column(
        Enum("company", "order", name="qb_entity_type"), nullable=False
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # Status: pending | success | failed | retry
    status: Mapped[str] = mapped_column(
        Enum("pending", "success", "failed", "retry", name="qb_sync_status"),
        default="pending",
        nullable=False,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    qb_entity_id: Mapped[str | None] = mapped_column(String(255))


class WebhookLog(BaseModel):
    """Idempotency log for inbound webhooks (Stripe, etc.)."""

    __tablename__ = "webhook_log"

    event_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True,
        comment="Provider event ID (e.g. Stripe evt_xxx) for deduplication"
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, comment="Raw JSON payload")
    # Status: received | processed | failed
    status: Mapped[str] = mapped_column(
        Enum("received", "processed", "failed", name="webhook_status"),
        default="received",
        nullable=False,
    )
    error_message: Mapped[str | None] = mapped_column(Text)


class AppSettings(Base):
    """Simple key-value store for runtime configuration (e.g. QB tokens)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PriceListRequest(BaseModel):
    """Tracks async price list generation jobs."""

    __tablename__ = "price_list_requests"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    format: Mapped[str] = mapped_column(
        Enum("pdf", "excel", name="pricelist_format"), nullable=False
    )
    # Status: pending | processing | completed | failed
    status: Mapped[str] = mapped_column(
        Enum("pending", "processing", "completed", "failed", name="pricelist_status"),
        default="pending",
        nullable=False,
    )
    file_url: Mapped[str | None] = mapped_column(String(1000))
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
