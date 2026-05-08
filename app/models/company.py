"""Company, CompanyUser, Contact, and UserAddress models."""
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.pricing import PricingTier
    from app.models.shipping import ShippingTier
    from app.models.order import Order
    from app.models.wholesale import WholesaleApplication


class Company(BaseModel):
    """Wholesale buyer company account."""

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trading_name: Mapped[str | None] = mapped_column(String(255))
    tax_id: Mapped[str | None] = mapped_column(String(100))
    business_type: Mapped[str | None] = mapped_column(String(100))
    website: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(50))

    # Status: pending | active | suspended | rejected
    status: Mapped[str] = mapped_column(
        Enum("pending", "active", "suspended", "rejected", name="company_status"),
        default="pending",
        nullable=False,
        index=True,
    )

    # Pricing / Shipping tier assignments
    pricing_tier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pricing_tiers.id", ondelete="SET NULL"), index=True
    )
    shipping_tier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shipping_tiers.id", ondelete="SET NULL"), index=True
    )
    shipping_override_amount: Mapped[float | None] = mapped_column(
        Numeric(10, 2), comment="Fixed shipping cost override. NULL = use tier brackets."
    )

    # QuickBooks
    qb_customer_id: Mapped[str | None] = mapped_column(String(255))
    default_payment_method_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Stripe
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Extended profile fields
    fax: Mapped[str | None] = mapped_column(String(50))
    tax_id_expiry: Mapped[str | None] = mapped_column(String(20))
    secondary_business: Mapped[str | None] = mapped_column(String(100))
    estimated_annual_volume: Mapped[str | None] = mapped_column(String(50))
    ppac_number: Mapped[str | None] = mapped_column(String(100))
    ppai_number: Mapped[str | None] = mapped_column(String(100))
    asi_number: Mapped[str | None] = mapped_column(String(100))

    # Registration-form fields (added from wholesale application form)
    company_email: Mapped[str | None] = mapped_column(String(255))
    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(100))
    state_province: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str | None] = mapped_column(String(100))
    how_heard: Mapped[str | None] = mapped_column(String(100))
    num_employees: Mapped[str | None] = mapped_column(String(50))
    num_sales_reps: Mapped[str | None] = mapped_column(String(50))

    # Notes & Tags
    admin_notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSONB, default=list, server_default="'[]'::jsonb")

    # ACH/bank account on file (informational — for reference, not live processing)
    ach_account: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Tax exemption flag — when True, no tax is charged or shown at checkout
    tax_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    # ── Relationships ─────────────────────────────────────────────────────────
    users: Mapped[list["CompanyUser"]] = relationship(
        "CompanyUser", back_populates="company", cascade="all, delete-orphan"
    )
    contacts: Mapped[list["Contact"]] = relationship(
        "Contact", back_populates="company", cascade="all, delete-orphan"
    )
    addresses: Mapped[list["UserAddress"]] = relationship(
        "UserAddress", back_populates="company", cascade="all, delete-orphan"
    )
    pricing_tier: Mapped["PricingTier | None"] = relationship("PricingTier")
    shipping_tier: Mapped["ShippingTier | None"] = relationship("ShippingTier")
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="company")
    wholesale_application: Mapped["WholesaleApplication | None"] = relationship(
        "WholesaleApplication", back_populates="company", uselist=False
    )


class CompanyUser(BaseModel):
    """Junction table: User ↔ Company with role."""

    __tablename__ = "company_users"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Role: owner | buyer | viewer | finance
    role: Mapped[str] = mapped_column(
        Enum("owner", "buyer", "viewer", "finance", name="company_user_role"),
        default="buyer",
        nullable=False,
    )
    user_group: Mapped[str] = mapped_column(String(50), default="Users", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    invited_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    company: Mapped["Company"] = relationship("Company", back_populates="users")
    user: Mapped["User"] = relationship("User", back_populates="company_memberships", foreign_keys=[user_id])


class Contact(BaseModel):
    """Company contact person for notifications."""

    __tablename__ = "contacts"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Contact Entry
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    department: Mapped[str | None] = mapped_column(String(50))
    time_zone: Mapped[str | None] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(50))
    phone_ext: Mapped[str | None] = mapped_column(String(20))
    fax: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    web_address: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)

    # Contact Detail / Home Address
    home_address1: Mapped[str | None] = mapped_column(String(255))
    home_address2: Mapped[str | None] = mapped_column(String(255))
    home_postal_code: Mapped[str | None] = mapped_column(String(20))
    home_city: Mapped[str | None] = mapped_column(String(100))
    home_state: Mapped[str | None] = mapped_column(String(100))
    home_country: Mapped[str | None] = mapped_column(String(2), default="US")
    home_phone: Mapped[str | None] = mapped_column(String(50))
    home_fax: Mapped[str | None] = mapped_column(String(50))
    home_email: Mapped[str | None] = mapped_column(String(255))
    alt_contacts: Mapped[str | None] = mapped_column(Text)

    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Notification preferences
    notify_order_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_order_shipped: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_invoices: Mapped[bool] = mapped_column(Boolean, default=False)

    company: Mapped["Company"] = relationship("Company", back_populates="contacts")


class UserAddress(BaseModel):
    """Shipping address belonging to a company."""

    __tablename__ = "user_addresses"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "Warehouse", "HQ"
    full_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False)
    country: Mapped[str] = mapped_column(String(2), default="US", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    company: Mapped["Company"] = relationship("Company", back_populates="addresses")
