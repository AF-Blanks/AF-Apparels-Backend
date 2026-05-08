"""Discount group and variant pricing override models."""
from sqlalchemy import Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class DiscountGroup(BaseModel):
    __tablename__ = "discount_groups"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    customer_tag: Mapped[str | None] = mapped_column(String(100))
    applies_to: Mapped[str] = mapped_column(String(20), nullable=False, server_default="'store'")
    applies_to_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_req_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="'none'")
    min_req_value: Mapped[float | None] = mapped_column(Numeric(12, 2))
    shipping_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default="'store_default'")
    shipping_amount: Mapped[float | None] = mapped_column(Numeric(10, 2), server_default="0")
    shipping_calc_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    shipping_cutoff_time: Mapped[str | None] = mapped_column(String(50), nullable=True)
    shipping_brackets_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="'enabled'")


class VariantPricingOverride(BaseModel):
    __tablename__ = "variant_pricing_overrides"

    product_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tier_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
    discount_percent: Mapped[float | None] = mapped_column(Numeric(5, 2))


class VariantLevelPricingOverride(BaseModel):
    """Per-variant, per-group price override — supersedes product-level override for the specific variant."""

    __tablename__ = "variant_level_pricing_overrides"

    variant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    group_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    price: Mapped[float | None] = mapped_column(Numeric(10, 2))
