"""Order, OrderItem, CartItem, AbandonedCart, OrderTemplate models."""
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.company import Company, UserAddress
    from app.models.product import ProductVariant
    from app.models.user import User


class Order(BaseModel):
    __tablename__ = "orders"

    order_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=True, index=True
    )
    placed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    # Guest order fields
    guest_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    guest_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    guest_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_guest_order: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Status: pending | confirmed | processing | ready_for_pickup | shipped | delivered | cancelled | refunded
    status: Mapped[str] = mapped_column(
        Enum("pending", "confirmed", "processing", "ready_for_pickup", "shipped", "delivered", "cancelled", "refunded",
             name="order_status"),
        default="pending",
        nullable=False,
        index=True,
    )

    po_number: Mapped[str | None] = mapped_column(String(100), index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    # Shipping address snapshot
    shipping_address_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_addresses.id", ondelete="SET NULL")
    )
    shipping_address_snapshot: Mapped[str | None] = mapped_column(
        Text, comment="JSON snapshot of address at time of order"
    )

    # Financials (all snapshotted at order creation)
    subtotal: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    shipping_cost: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    tax_amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    tax_rate: Mapped[float | None] = mapped_column(Numeric(6, 4), nullable=True)
    tax_region: Mapped[str | None] = mapped_column(String(10), nullable=True)
    total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # Payment
    payment_status: Mapped[str] = mapped_column(
        Enum("unpaid", "pending", "paid", "refunded", "failed", name="payment_status"),
        default="unpaid",
        nullable=False,
    )
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(255), index=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255))

    # Shipping
    shipping_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(255))
    carrier: Mapped[str | None] = mapped_column(String(100))
    courier: Mapped[str | None] = mapped_column(String(100))
    courier_service: Mapped[str | None] = mapped_column(String(100))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # QuickBooks Payments
    qb_payment_charge_id: Mapped[str | None] = mapped_column(String(255), index=True)
    qb_payment_status: Mapped[str | None] = mapped_column(String(50))

    # QuickBooks sync
    qb_sync_status: Mapped[str] = mapped_column(
        Enum("pending", "synced", "failed", "skipped", name="qb_order_sync_status"),
        default="pending",
        nullable=False,
    )
    qb_invoice_id: Mapped[str | None] = mapped_column(String(255))

    # Payment method + ACH details (added after initial deployment — saved via raw SQL)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ach_bank_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ach_account_holder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ach_routing_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ach_account_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    ach_account_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ach_verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)

    # ── Schema compatibility aliases ────────────────────────────────────────────
    @property
    def order_notes(self) -> str | None:
        return self.notes

    @property
    def item_count(self) -> int:
        return len(self.items) if self.items is not None else 0

    # ── Relationships ─────────────────────────────────────────────────────────
    company: Mapped[Optional["Company"]] = relationship("Company", back_populates="orders")
    placed_by: Mapped[Optional["User"]] = relationship("User")
    items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )
    comments: Mapped[list["OrderComment"]] = relationship(
        "OrderComment", back_populates="order", cascade="all, delete-orphan",
        order_by="OrderComment.created_at"
    )


class OrderItem(BaseModel):
    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, comment="Price snapshotted at time of order"
    )
    line_total: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    # Denormalized product info for historical accuracy
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[str | None] = mapped_column(String(50))

    order: Mapped["Order"] = relationship("Order", back_populates="items")
    variant: Mapped[Optional["ProductVariant"]] = relationship("ProductVariant")


# class CartItem(BaseModel):
#     """Live cart item stored in DB (user_id + variant_id)."""

#     __tablename__ = "cart_items"

#     user_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
#         nullable=False, index=True
#     )
#     variant_id: Mapped[uuid.UUID] = mapped_column(
#         UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
#     )
#     quantity: Mapped[int] = mapped_column(Integer, nullable=False)
#     price_at_add: Mapped[float | None] = mapped_column(
#         Numeric(10, 2), comment="Tier price at time item was added"
#     )

#     user: Mapped["User"] = relationship("User")
#     variant: Mapped["ProductVariant"] = relationship("ProductVariant")

class CartItem(BaseModel):
    """Live cart item stored in DB (company_id + variant_id)."""

    __tablename__ = "cart_items"

    # CHANGE: user_id → company_id (B2B cart belongs to company)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float | None] = mapped_column(  # price_at_add rename
        Numeric(10, 2), comment="Tier price at time item was added"
    )

    # Relationships
    company: Mapped["Company"] = relationship("Company")
    variant: Mapped["ProductVariant"] = relationship("ProductVariant")


class AbandonedCart(BaseModel):
    """Snapshot of an inactive company cart for analytics / re-engagement."""

    __tablename__ = "abandoned_carts"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    items_snapshot: Mapped[str] = mapped_column(Text, nullable=False, comment="JSON snapshot of cart items")
    total: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    abandoned_at: Mapped[str] = mapped_column(String(50), nullable=False)
    is_recovered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recovered_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    recovery_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )

    company: Mapped["Company"] = relationship("Company")
    user: Mapped["User | None"] = relationship("User")


class OrderComment(BaseModel):
    """Buyer-visible comment or note on an order (from admin or buyer)."""

    __tablename__ = "order_comments"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    order: Mapped["Order"] = relationship("Order", back_populates="comments")
    author: Mapped["User | None"] = relationship("User")


class OrderTemplate(BaseModel):
    """Named saved cart (SKU + quantity pairs)."""

    __tablename__ = "order_templates"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    items: Mapped[str] = mapped_column(
        Text, nullable=False, comment="JSON array of {sku, quantity}"
    )

    company: Mapped["Company"] = relationship("Company")
    created_by: Mapped["User"] = relationship("User")
