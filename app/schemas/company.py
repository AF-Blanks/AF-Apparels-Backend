from uuid import UUID
from decimal import Decimal
from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class CompanyListItem(BaseModel):
    id: UUID
    name: str
    status: str
    pricing_tier_id: UUID | None
    shipping_tier_id: UUID | None
    order_count: int = 0
    total_spend: Decimal = Decimal("0")
    created_at: datetime
    # Contact info (enriched from owner user)
    email: str | None = None
    phone: str | None = None
    contact_name: str | None = None
    last_order_date: datetime | None = None
    tags: list[str] = []

    model_config = {"from_attributes": True}

    @field_validator("tags", mode="before")
    @classmethod
    def coerce_tags(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(t) for t in v]
        return []


class CompanyDetail(BaseModel):
    id: UUID
    name: str
    status: str
    tax_id: str | None
    business_type: str | None
    website: str | None
    phone: str | None = None
    fax: str | None = None
    secondary_business: str | None = None
    estimated_annual_volume: str | None = None
    ppac_number: str | None = None
    ppai_number: str | None = None
    asi_number: str | None = None
    # Registration fields
    company_email: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country: str | None = None
    how_heard: str | None = None
    num_employees: str | None = None
    num_sales_reps: str | None = None
    pricing_tier_id: UUID | None
    shipping_tier_id: UUID | None
    shipping_override_amount: Decimal | None
    stripe_customer_id: str | None
    qb_customer_id: str | None
    admin_notes: str | None = None
    tags: list[str] = []
    discount_percent: Decimal | None = None
    tax_exempt: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("tags", mode="before")
    @classmethod
    def coerce_tags(cls, v: object) -> list[str]:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(t) for t in v]
        return []


class CompanyUpdate(BaseModel):
    pricing_tier_id: UUID | None = None
    shipping_tier_id: UUID | None = None
    shipping_override_amount: Decimal | None = None
    status: str | None = None
    admin_notes: str | None = None
    tags: list[str] | None = None
    tax_exempt: bool | None = None


class SuspendRequest(BaseModel):
    reason: str = Field(..., min_length=1)
