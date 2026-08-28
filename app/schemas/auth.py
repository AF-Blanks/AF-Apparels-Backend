"""Auth Pydantic schemas."""
from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterWholesaleRequest(BaseModel):
    # Company info
    company_name: str = Field(..., min_length=2, max_length=255)
    tax_id: str | None = Field(None, max_length=100)
    business_type: str = Field(..., min_length=2, max_length=100)
    website: str | None = Field(None, max_length=500)
    expected_monthly_volume: str | None = None

    # Extended company info (registration form)
    fax: str | None = Field(None, max_length=50)
    secondary_business: str | None = Field(None, max_length=255)
    estimated_annual_volume: str | None = Field(None, max_length=100)
    ppac_number: str | None = Field(None, max_length=100)
    ppai_number: str | None = Field(None, max_length=100)
    asi_number: str | None = Field(None, max_length=100)
    company_email: str | None = Field(None, max_length=255)
    address_line1: str | None = Field(None, max_length=255)
    address_line2: str | None = Field(None, max_length=255)
    city: str | None = Field(None, max_length=100)
    state_province: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)
    # Asked of every applicant, and enforced here as well as on the form: the
    # browser's own check is the first thing skipped by anything posting
    # straight to the API, and an application that arrives without it cannot be
    # chased for it afterwards. min_length so an empty answer is not an answer.
    how_heard: str = Field(..., min_length=1, max_length=100)

    # Extra mailboxes that should also receive this customer's order paperwork
    # (a buyer, an accounts inbox, a warehouse). The direct and company emails
    # above are always included, so this is only the additions.
    additional_emails: list[str] = Field(default_factory=list)
    num_employees: str | None = Field(None, max_length=50)
    num_sales_reps: str | None = Field(None, max_length=50)

    # Contact info
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = Field(None, max_length=50)
    password: str = Field(..., min_length=8)


class TokenRefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class ActivateAccountSchema(BaseModel):
    token: str
    # Personal
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    phone: str | None = Field(None, max_length=50)
    # Business
    company_name: str = Field(..., min_length=1, max_length=255)
    business_type: str = Field(..., min_length=1, max_length=100)
    website: str | None = Field(None, max_length=500)
    tax_id: str | None = Field(None, max_length=100)
    company_email: str | None = Field(None, max_length=255)
    # Address
    address_line1: str | None = Field(None, max_length=255)
    address_line2: str | None = Field(None, max_length=255)
    city: str | None = Field(None, max_length=100)
    state_province: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)
    # Account
    password: str = Field(..., min_length=8)
    confirm_password: str
    # Additional
    how_heard: str | None = Field(None, max_length=100)
    secondary_business: str | None = Field(None, max_length=255)
    num_employees: str | None = Field(None, max_length=50)
    num_sales_reps: str | None = Field(None, max_length=50)


class ResendActivationSchema(BaseModel):
    email: EmailStr
