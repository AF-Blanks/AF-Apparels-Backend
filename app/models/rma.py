"""Return Merchandise Authorization (RMA) models."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.order import Order, OrderItem
    from app.models.user import User


class RMARequest(BaseModel):
    __tablename__ = "rma_requests"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False, index=True
    )
    submitted_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    rma_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)

    # Status: pending | approved | rejected | completed
    status: Mapped[str] = mapped_column(
        Enum("pending", "approved", "rejected", "completed", name="rma_status"),
        default="pending",
        nullable=False,
        index=True,
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    admin_notes: Mapped[str | None] = mapped_column(Text)

    # Set when an "approved" transition triggers refund + restock.
    # refund_status / restock_status: null | pending | done | failed
    # (refund also allows "not_applicable" — no card charge to refund, e.g. Net-30 unpaid)
    refund_status: Mapped[str | None] = mapped_column(String(20))
    refund_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))
    qb_refund_id: Mapped[str | None] = mapped_column(String(255))
    restock_status: Mapped[str | None] = mapped_column(String(20))
    restocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[str | None] = mapped_column(Text)

    order: Mapped["Order"] = relationship("Order")
    submitted_by: Mapped["User"] = relationship("User")
    items: Mapped[list["RMAItem"]] = relationship(
        "RMAItem", back_populates="rma", cascade="all, delete-orphan"
    )


class RMAItem(BaseModel):
    __tablename__ = "rma_items"

    rma_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rma_requests.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    order_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("order_items.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    refund_amount: Mapped[float | None] = mapped_column(Numeric(10, 2))

    rma: Mapped["RMARequest"] = relationship("RMARequest", back_populates="items")
    order_item: Mapped["OrderItem"] = relationship("OrderItem")
