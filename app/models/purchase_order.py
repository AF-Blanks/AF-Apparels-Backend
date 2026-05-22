"""Purchase Order models: Manufacturer, PurchaseOrder, POLineItem, POReceiving, POReceivingItem."""
import uuid
from datetime import date as dt_date
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, Computed, Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.product import ProductVariant


class Manufacturer(BaseModel):
    __tablename__ = "manufacturers"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(
        "PurchaseOrder", back_populates="manufacturer"
    )


class PurchaseOrder(BaseModel):
    __tablename__ = "purchase_orders"

    # DB trigger generates this on INSERT; default="" so SQLAlchemy sends something the trigger can overwrite
    po_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True, default="")
    manufacturer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("manufacturers.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(50), default="draft", nullable=False)
    order_date: Mapped[dt_date | None] = mapped_column(Date, nullable=True)
    expected_delivery: Mapped[dt_date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_expected: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    total_received: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    qb_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    qb_po_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    qb_bill_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    manufacturer: Mapped[Optional["Manufacturer"]] = relationship(
        "Manufacturer", back_populates="purchase_orders"
    )
    line_items: Mapped[list["POLineItem"]] = relationship(
        "POLineItem", back_populates="purchase_order", cascade="all, delete-orphan"
    )
    receivings: Mapped[list["POReceiving"]] = relationship(
        "POReceiving", back_populates="purchase_order", cascade="all, delete-orphan"
    )


class POLineItem(BaseModel):
    __tablename__ = "po_line_items"

    po_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    product_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("product_variants.id"), nullable=True
    )
    new_product_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_product_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_product_size: Mapped[str | None] = mapped_column(String(50), nullable=True)
    new_product_color: Mapped[str | None] = mapped_column(String(50), nullable=True)
    qty_ordered: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_cost_expected: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    total_expected: Mapped[float | None] = mapped_column(
        Numeric(10, 2),
        Computed("qty_ordered * unit_cost_expected", persisted=True),
        nullable=True,
    )

    purchase_order: Mapped["PurchaseOrder"] = relationship("PurchaseOrder", back_populates="line_items")
    variant: Mapped[Optional["ProductVariant"]] = relationship("ProductVariant")
    receiving_items: Mapped[list["POReceivingItem"]] = relationship(
        "POReceivingItem", back_populates="line_item"
    )


class POReceiving(BaseModel):
    __tablename__ = "po_receivings"

    po_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    received_date: Mapped[dt_date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    qb_bill_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    qb_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    purchase_order: Mapped["PurchaseOrder"] = relationship("PurchaseOrder", back_populates="receivings")
    items: Mapped[list["POReceivingItem"]] = relationship(
        "POReceivingItem", back_populates="receiving", cascade="all, delete-orphan"
    )


class POReceivingItem(BaseModel):
    __tablename__ = "po_receiving_items"

    receiving_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("po_receivings.id", ondelete="CASCADE"), nullable=False
    )
    po_line_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("po_line_items.id"), nullable=True
    )
    qty_received: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit_cost_actual: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    total_actual: Mapped[float | None] = mapped_column(
        Numeric(10, 2),
        Computed("qty_received * unit_cost_actual", persisted=True),
        nullable=True,
    )

    receiving: Mapped["POReceiving"] = relationship("POReceiving", back_populates="items")
    line_item: Mapped[Optional["POLineItem"]] = relationship("POLineItem", back_populates="receiving_items")
