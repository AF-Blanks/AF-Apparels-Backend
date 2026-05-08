"""Wholesale application Pydantic schemas."""
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class WholesaleApplicationOut(BaseModel):
    id: uuid.UUID
    # Core company info
    company_name: str
    tax_id: str | None
    business_type: str
    website: str | None
    expected_monthly_volume: str | None
    # Extended registration fields
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
    secondary_business: str | None = None
    estimated_annual_volume: str | None = None
    ppac_number: str | None = None
    ppai_number: str | None = None
    asi_number: str | None = None
    fax: str | None = None
    # Contact info
    first_name: str
    last_name: str
    email: str
    phone: str | None
    # Status
    status: str
    rejection_reason: str | None
    admin_notes: str | None
    company_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApproveApplicationRequest(BaseModel):
    pricing_tier_id: uuid.UUID | None = None
    shipping_tier_id: uuid.UUID | None = None
    discount_group_id: uuid.UUID | None = None
    admin_notes: str | None = None
    tax_exempt: bool = False


class RejectApplicationRequest(BaseModel):
    rejection_reason: str = Field(..., min_length=10, max_length=1000)
    admin_notes: str | None = None
