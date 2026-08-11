from datetime import datetime
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field


class ProfileOut(BaseModel):
    id: UUID
    email: str
    first_name: str
    last_name: str
    phone: str | None
    is_active: bool
    email_verified: bool
    created_at: datetime
    # Current role, straight from the database — the JWT carries whatever role it
    # was minted with, so the app refreshes from here to notice a change.
    is_admin: bool = False
    is_staff: bool = False

    model_config = {"from_attributes": True}


class ProfileUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None


class CompanyProfileUpdate(BaseModel):
    name: str | None = None
    trading_name: str | None = None
    phone: str | None = None
    fax: str | None = None
    website: str | None = None
    tax_id: str | None = None
    tax_id_expiry: str | None = None
    business_type: str | None = None
    secondary_business: str | None = None
    estimated_annual_volume: str | None = None
    ppac_number: str | None = None
    ppai_number: str | None = None
    asi_number: str | None = None
    # Registration form fields
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


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class CompanyUserOut(BaseModel):
    id: UUID
    user_id: UUID
    email: str
    first_name: str
    last_name: str
    role: str
    user_group: str
    is_active: bool

    model_config = {"from_attributes": True}


class UserInvite(BaseModel):
    first_name: str
    last_name: str
    email: str
    role: str = "buyer"
    user_group: str = "Users"
    password: str
    password_hint: str | None = None


class UserUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    role: str | None = None
    user_group: str | None = None
    is_active: bool | None = None


class RoleUpdate(BaseModel):
    role: str = Field(..., pattern="^(owner|buyer|viewer|finance)$")


class ContactCreate(BaseModel):
    # Contact Entry
    first_name: str
    last_name: str
    department: str | None = None
    time_zone: str | None = None
    phone: str | None = None
    phone_ext: str | None = None
    fax: str | None = None
    email: str
    web_address: str | None = None
    notes: str | None = None
    # Contact Detail
    home_address1: str | None = None
    home_address2: str | None = None
    home_postal_code: str | None = None
    home_city: str | None = None
    home_state: str | None = None
    home_country: str | None = "US"
    home_phone: str | None = None
    home_fax: str | None = None
    home_email: str | None = None
    alt_contacts: str | None = None
    # Notifications
    notify_order_confirmation: bool = True
    notify_order_shipped: bool = True
    notify_invoices: bool = False
    is_primary: bool = False


class ContactOut(ContactCreate):
    id: UUID

    model_config = {"from_attributes": True}


class StatementOut(BaseModel):
    id: UUID
    period_start: datetime
    period_end: datetime
    total_orders: int
    total_amount: Decimal
    status: str  # open | closed
    pdf_url: str | None

    model_config = {"from_attributes": True}


class MessageCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1)
    parent_id: UUID | None = None


class MessageOut(BaseModel):
    id: UUID
    subject: str
    body: str
    parent_id: UUID | None
    is_read_by_company: bool
    created_at: datetime

    model_config = {"from_attributes": True}
