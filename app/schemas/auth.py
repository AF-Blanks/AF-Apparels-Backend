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
    how_heard: str | None = Field(None, max_length=100)
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
