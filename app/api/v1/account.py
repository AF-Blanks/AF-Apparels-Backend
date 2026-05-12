"""Account endpoints — profile, addresses, payment methods, price list, and more.

Each phase adds endpoints to this router:
  Phase 4 (T056): price list generation + status
  Phase 9 (T094–T095): addresses + payment methods
  Phase 15 (T143–T149): profile, users, contacts, statements, messages, inventory report
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import ForbiddenError

router = APIRouter(prefix="/account", tags=["account"])


# ---------------------------------------------------------------------------
# Price list (T056 — US-10)
# ---------------------------------------------------------------------------

from app.schemas.system import PriceListRequestOut  # noqa: E402


@router.post(
    "/price-list",
    response_model=PriceListRequestOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_price_list(
    request: Request,
    format: str = Query("pdf", pattern="^(pdf|excel)$"),
    db: AsyncSession = Depends(get_db),
):
    """Queue async price list generation. Returns request_id for polling."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    from app.services.pricelist_service import PriceListService

    svc = PriceListService(db)
    req = await svc.request_generation(company_id=company_id, format=format)
    await db.commit()
    return req


@router.get("/price-list/{request_id}", response_model=PriceListRequestOut)
async def get_price_list_status(
    request_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Poll price list generation status. Returns file_url when completed."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    from app.services.pricelist_service import PriceListService

    svc = PriceListService(db)
    return await svc.get_request_status(request_id=request_id, company_id=company_id)


# ---------------------------------------------------------------------------
# Addresses (T094 — US-6)
# ---------------------------------------------------------------------------

from app.schemas.order import AddressIn, AddressOut  # noqa: E402
from app.models.company import UserAddress  # noqa: E402
from sqlalchemy import func, select, delete, update  # noqa: E402


@router.get("/addresses", response_model=list[AddressOut])
async def list_addresses(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    result = await db.execute(
        select(UserAddress).where(UserAddress.company_id == company_id)
    )
    return result.scalars().all()


@router.post("/addresses", response_model=AddressOut, status_code=status.HTTP_201_CREATED)
async def create_address(
    payload: AddressIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    count = (await db.execute(
        select(func.count(UserAddress.id)).where(UserAddress.company_id == company_id)
    )).scalar_one()

    make_default = payload.is_default or count == 0
    if make_default:
        await db.execute(
            update(UserAddress)
            .where(UserAddress.company_id == company_id)
            .values(is_default=False)
        )

    addr = UserAddress(
        company_id=company_id,
        label=payload.label,
        full_name=payload.full_name,
        address_line1=payload.line1,
        address_line2=payload.line2,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        country=payload.country,
        phone=payload.phone,
        is_default=make_default,
    )
    db.add(addr)
    await db.commit()
    await db.refresh(addr)
    try:
        user_id = getattr(request.state, "user_id", None)
        if user_id:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            if user:
                from app.services.email_service import EmailService
                EmailService(db).send_raw(
                    to_email=user.email,
                    subject="New address added — AF Apparels",
                    body_html=(
                        f"<h2>Address Added</h2>"
                        f"<p>Hi {user.first_name or 'there'},</p>"
                        f"<p>A new shipping address has been added to your account: <strong>{payload.label or payload.full_name}</strong>.</p>"
                        f"<p>If you did not make this change, please contact us immediately.</p>"
                        f"<br><p>AF Apparels Team</p>"
                    ),
                )
    except Exception:
        pass
    return addr


@router.patch("/addresses/{address_id}", response_model=AddressOut)
async def update_address(
    address_id: UUID,
    payload: AddressIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.core.exceptions import NotFoundError
    addr = (await db.execute(
        select(UserAddress).where(
            UserAddress.id == address_id, UserAddress.company_id == company_id
        )
    )).scalar_one_or_none()
    if not addr:
        raise NotFoundError("Address not found")

    if payload.is_default and not addr.is_default:
        await db.execute(
            update(UserAddress)
            .where(UserAddress.company_id == company_id)
            .values(is_default=False)
        )

    addr.label = payload.label
    addr.full_name = payload.full_name
    addr.address_line1 = payload.line1
    addr.address_line2 = payload.line2
    addr.city = payload.city
    addr.state = payload.state
    addr.postal_code = payload.postal_code
    addr.country = payload.country
    addr.phone = payload.phone
    addr.is_default = payload.is_default
    await db.commit()
    await db.refresh(addr)
    return addr


@router.patch("/addresses/{address_id}/set-default", response_model=AddressOut)
async def set_default_address(
    address_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.core.exceptions import NotFoundError
    await db.execute(
        update(UserAddress)
        .where(UserAddress.company_id == company_id)
        .values(is_default=False)
    )
    addr = (await db.execute(
        select(UserAddress).where(
            UserAddress.id == address_id, UserAddress.company_id == company_id
        )
    )).scalar_one_or_none()
    if not addr:
        raise NotFoundError("Address not found")
    addr.is_default = True
    await db.commit()
    await db.refresh(addr)
    return addr


@router.delete("/addresses/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(
    address_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    addr = (await db.execute(
        select(UserAddress).where(UserAddress.id == address_id, UserAddress.company_id == company_id)
    )).scalar_one_or_none()
    await db.execute(
        delete(UserAddress).where(
            UserAddress.id == address_id, UserAddress.company_id == company_id
        )
    )
    await db.commit()
    try:
        user_id = getattr(request.state, "user_id", None)
        if user_id and addr:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            if user:
                from app.services.email_service import EmailService
                EmailService(db).send_raw(
                    to_email=user.email,
                    subject="Address removed — AF Apparels",
                    body_html=(
                        f"<h2>Address Removed</h2>"
                        f"<p>Hi {user.first_name or 'there'},</p>"
                        f"<p>A shipping address has been removed from your account: <strong>{addr.label or addr.full_name}</strong>.</p>"
                        f"<p>If you did not make this change, please contact us immediately.</p>"
                        f"<br><p>AF Apparels Team</p>"
                    ),
                )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Payment methods (T095 — US-6)
# ---------------------------------------------------------------------------

@router.get("/payment-methods")
async def list_payment_methods(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.models.company import Company
    from app.services.qb_payments_service import QBPaymentsService

    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company or not company.qb_customer_id:
        return []

    try:
        svc = QBPaymentsService()
        cards = svc.list_saved_cards(company.qb_customer_id)
        default_id = company.default_payment_method_id
        return [
            {
                "id": card.get("id"),
                "brand": card.get("cardType", "Unknown"),
                "last4": (card.get("number") or "")[-4:] or "****",
                "exp_month": card.get("expMonth"),
                "exp_year": card.get("expYear"),
                "name": card.get("name"),
                "billing_address": card.get("address"),
                "is_default": card.get("id") == default_id,
                "created": card.get("created"),
            }
            for card in (cards if isinstance(cards, list) else [])
        ]
    except Exception:
        return []


@router.post("/payment-methods", status_code=status.HTTP_201_CREATED)
async def add_payment_method(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Save a new card to the QB customer wallet from raw card fields."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.models.company import Company
    from app.services.qb_payments_service import QBPaymentsService
    import uuid as _uuid

    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise ForbiddenError("Company not found")

    svc = QBPaymentsService()

    # Ensure the company has a QB customer profile
    if not company.qb_customer_id:
        qb_id = svc.create_customer(str(_uuid.uuid4()))
        company.qb_customer_id = qb_id
        await db.commit()
        await db.refresh(company)

    card = payload.get("card", {})
    try:
        saved = svc.save_card(
            customer_id=company.qb_customer_id,
            card_number=card.get("number", ""),
            exp_month=card.get("expMonth", ""),
            exp_year=card.get("expYear", ""),
            cvc=card.get("cvc", ""),
            name=card.get("name") or None,
        )
        # Set as default if no default exists
        if not company.default_payment_method_id:
            company.default_payment_method_id = saved.get("id")
            await db.commit()
        result = {
            "id": saved.get("id"),
            "brand": saved.get("cardType", "Unknown"),
            "last4": (saved.get("number") or "")[-4:] or "****",
            "exp_month": saved.get("expMonth"),
            "exp_year": saved.get("expYear"),
            "name": saved.get("name"),
            "is_default": not company.default_payment_method_id or company.default_payment_method_id == saved.get("id"),
        }
        try:
            user_id = getattr(request.state, "user_id", None)
            if user_id:
                user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
                if user:
                    from app.services.email_service import EmailService
                    last4 = result["last4"]
                    brand = result["brand"]
                    EmailService(db).send_raw(
                        to_email=user.email,
                        subject="Payment method added — AF Apparels",
                        body_html=(
                            f"<h2>Payment Method Added</h2>"
                            f"<p>Hi {user.first_name or 'there'},</p>"
                            f"<p>A new payment method has been added to your account: <strong>{brand} ending in {last4}</strong>.</p>"
                            f"<p>If you did not make this change, please contact us immediately.</p>"
                            f"<br><p>AF Apparels Team</p>"
                        ),
                    )
        except Exception:
            pass
        return result
    except Exception as exc:
        raise ForbiddenError(f"Failed to save card: {exc}")


@router.patch("/payment-methods/{payment_method_id}/set-default")
async def set_default_payment_method(
    payment_method_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.models.company import Company

    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if company:
        company.default_payment_method_id = payment_method_id
        await db.commit()
    return {"message": "Default payment method updated"}


@router.delete("/payment-methods/{payment_method_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment_method(
    payment_method_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.models.company import Company
    from app.services.qb_payments_service import QBPaymentsService

    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company or not company.qb_customer_id:
        return

    try:
        svc = QBPaymentsService()
        svc.delete_saved_card(company.qb_customer_id, payment_method_id)
        if company.default_payment_method_id == payment_method_id:
            company.default_payment_method_id = None
            await db.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# ACH / bank account on file
# ---------------------------------------------------------------------------

@router.get("/ach-method")
async def get_ach_method(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.models.company import Company
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise ForbiddenError("Company account required")
    return company.ach_account or {}


@router.put("/ach-method")
async def save_ach_method(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.models.company import Company
    body = await request.json()

    allowed_fields = {"bank_name", "account_holder", "routing_last4", "account_last4", "account_type"}
    ach = {k: str(v).strip() for k, v in body.items() if k in allowed_fields and v is not None}

    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise ForbiddenError("Company account required")

    company.ach_account = ach or None
    await db.commit()
    return company.ach_account or {}


@router.delete("/ach-method", status_code=204)
async def delete_ach_method(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from app.models.company import Company
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if company:
        company.ach_account = None
        await db.commit()


# ---------------------------------------------------------------------------
# Profile (T144 — US-7)
# ---------------------------------------------------------------------------

from app.schemas.account import (  # noqa: E402
    ChangePasswordRequest, CompanyProfileUpdate, CompanyUserOut, ContactCreate,
    ContactOut, MessageCreate, MessageOut, ProfileOut, ProfileUpdate, RoleUpdate, UserInvite,
    UserUpdate,
)
from app.core.security import hash_password, verify_password  # noqa: E402
from app.core.exceptions import ConflictError, NotFoundError, ValidationError  # noqa: E402
from app.models.company import CompanyUser, Contact  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.communication import Message  # noqa: E402


@router.get("/profile", response_model=ProfileOut)
async def get_profile(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise ForbiddenError("Authentication required")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found")
    return user


@router.patch("/profile", response_model=ProfileOut)
async def update_profile(
    payload: ProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise ForbiddenError("Authentication required")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/profile/full")
async def get_full_profile(request: Request, db: AsyncSession = Depends(get_db)):
    """Return combined user + company profile for the account profile page."""
    user_id = getattr(request.state, "user_id", None)
    company_id = getattr(request.state, "company_id", None)
    if not user_id:
        raise ForbiddenError("Authentication required")

    from app.models.company import Company

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found")

    company = None
    if company_id:
        company = (await db.execute(
            select(Company).where(Company.id == company_id)
        )).scalar_one_or_none()

    return {
        "web_user": {
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
        },
        "company": {
            "account_number": str(company.id)[:8].upper(),
            "name": company.name,
            "trading_name": company.trading_name,
            "phone": company.phone,
            "fax": company.fax,
            "website": company.website,
            "tax_id": company.tax_id,
            "tax_id_expiry": company.tax_id_expiry,
            "business_type": company.business_type,
            "secondary_business": company.secondary_business,
            "estimated_annual_volume": company.estimated_annual_volume,
            "ppac_number": company.ppac_number,
            "ppai_number": company.ppai_number,
            "asi_number": company.asi_number,
            # Registration form fields
            "company_email": getattr(company, "company_email", None),
            "address_line1": getattr(company, "address_line1", None),
            "address_line2": getattr(company, "address_line2", None),
            "city": getattr(company, "city", None),
            "state_province": getattr(company, "state_province", None),
            "postal_code": getattr(company, "postal_code", None),
            "country": getattr(company, "country", None),
            "how_heard": getattr(company, "how_heard", None),
            "num_employees": getattr(company, "num_employees", None),
            "num_sales_reps": getattr(company, "num_sales_reps", None),
        } if company else None,
    }


@router.patch("/profile/user")
async def update_user_profile(
    payload: ProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update first_name / last_name / phone for the logged-in user."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise ForbiddenError("Authentication required")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    try:
        from app.services.email_service import EmailService
        EmailService(db).send_raw(
            to_email=user.email,
            subject="Your profile has been updated — AF Apparels",
            body_html=(
                f"<h2>Profile Updated</h2>"
                f"<p>Hi {user.first_name or 'there'},</p>"
                f"<p>Your AF Apparels account profile was successfully updated.</p>"
                f"<p>If you did not make this change, please contact us immediately.</p>"
                f"<br><p>AF Apparels Team</p>"
            ),
        )
    except Exception:
        pass
    return {"message": "User profile updated"}


@router.patch("/profile/company")
async def update_company_profile(
    payload: CompanyProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Update editable company profile fields."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    from app.models.company import Company

    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    if not company:
        raise NotFoundError("Company not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if hasattr(company, field):
            setattr(company, field, value)
    await db.commit()
    try:
        user_id = getattr(request.state, "user_id", None)
        if user_id:
            user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
            if user:
                from app.services.email_service import EmailService
                EmailService(db).send_raw(
                    to_email=user.email,
                    subject="Company profile updated — AF Apparels",
                    body_html=(
                        f"<h2>Company Profile Updated</h2>"
                        f"<p>Hi {user.first_name or 'there'},</p>"
                        f"<p>The company profile for <strong>{company.name}</strong> was successfully updated.</p>"
                        f"<p>If you did not make this change, please contact us immediately.</p>"
                        f"<br><p>AF Apparels Team</p>"
                    ),
                )
    except Exception:
        pass
    return {"message": "Company profile updated"}


@router.patch("/change-password", status_code=status.HTTP_200_OK)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise ForbiddenError("Authentication required")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found")
    if not verify_password(payload.current_password, user.hashed_password):
        raise ValidationError("Current password is incorrect")
    user.hashed_password = hash_password(payload.new_password)
    await db.commit()

    # Security notification — never block the response if email fails
    try:
        from app.services.email_service import EmailService
        EmailService(db).send_raw(
            to_email=user.email,
            subject="Your AF Apparels password has been changed",
            body_html=(
                f"<h2>Password Changed Successfully</h2>"
                f"<p>Hi {user.first_name},</p>"
                f"<p>Your AF Apparels account password was successfully changed.</p>"
                f"<p>If you did not make this change, please contact us immediately.</p>"
                f"<br><p>AF Apparels Team</p>"
            ),
        )
    except Exception:
        pass

    return {"message": "Password updated"}


# ---------------------------------------------------------------------------
# User management (T145 — US-7) — Owner role required
# ---------------------------------------------------------------------------

def _require_owner(request: Request) -> tuple:
    company_id = getattr(request.state, "company_id", None)
    company_role = getattr(request.state, "company_role", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    if company_role != "owner":
        raise ForbiddenError("Owner role required")
    return company_id, getattr(request.state, "user_id", None)


@router.get("/users", response_model=list[CompanyUserOut])
async def list_company_users(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    members = (await db.execute(
        select(CompanyUser).where(CompanyUser.company_id == company_id)
    )).scalars().all()
    out = []
    for m in members:
        user = (await db.execute(select(User).where(User.id == m.user_id))).scalar_one_or_none()
        if user:
            out.append(CompanyUserOut(
                id=m.id,
                user_id=m.user_id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                role=m.role,
                user_group=m.user_group,
                is_active=user.is_active,
            ))
    return out


@router.post("/users/invite", status_code=status.HTTP_201_CREATED)
async def invite_user(
    payload: UserInvite,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id, inviter_id = _require_owner(request)

    # Validate password
    import re
    if len(payload.password) < 8 or len(payload.password) > 20:
        raise ValidationError("Password must be between 8 and 20 characters")
    if not re.match(r"^[a-zA-Z]", payload.password):
        raise ValidationError("Password must begin with a letter")

    existing = (await db.execute(select(User).where(User.email == payload.email))).scalar_one_or_none()
    if existing:
        already_member = (await db.execute(
            select(CompanyUser).where(
                CompanyUser.company_id == company_id, CompanyUser.user_id == existing.id
            )
        )).scalar_one_or_none()
        if already_member:
            raise ConflictError("User already belongs to this company")
        user_id = existing.id
    else:
        new_user = User(
            email=payload.email,
            hashed_password=hash_password(payload.password),
            first_name=payload.first_name,
            last_name=payload.last_name,
            is_active=True,
            email_verified=False,
        )
        db.add(new_user)
        await db.flush()
        user_id = new_user.id

    db.add(CompanyUser(
        company_id=company_id,
        user_id=user_id,
        role=payload.role,
        user_group=payload.user_group,
        is_active=True,
        invited_by_id=inviter_id,
    ))
    await db.commit()

    try:
        from app.services.email_service import EmailService
        email_svc = EmailService(db)
        email_svc.send_raw(
            to_email=payload.email,
            subject="You have been invited to AF Apparels",
            body_html=f"""
            <h2>Welcome to AF Apparels!</h2>
            <p>Hi {payload.first_name},</p>
            <p>You have been invited to join the AF Apparels wholesale platform.</p>
            <p><strong>Your login details:</strong></p>
            <p>Email: {payload.email}<br>Password: {payload.password}</p>
            <p><a href="http://localhost:3000/login">Click here to login</a></p>
            <p>AF Apparels Team</p>
            """,
        )
    except Exception:
        pass

    return {"message": "User invited successfully", "user_id": str(user_id)}


@router.patch("/users/{user_id}", status_code=status.HTTP_200_OK)
async def update_company_user(
    user_id: UUID,
    payload: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id, _ = _require_owner(request)
    member = (await db.execute(
        select(CompanyUser).where(
            CompanyUser.company_id == company_id, CompanyUser.user_id == user_id
        )
    )).scalar_one_or_none()
    if not member:
        raise NotFoundError("User not found in company")

    if payload.role is not None:
        member.role = payload.role
    if payload.user_group is not None:
        member.user_group = payload.user_group

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user:
        if payload.first_name is not None:
            user.first_name = payload.first_name
        if payload.last_name is not None:
            user.last_name = payload.last_name
        if payload.is_active is not None:
            user.is_active = payload.is_active

    await db.commit()
    return {"message": "User updated"}


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_200_OK)
async def reset_user_password(
    user_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id, _ = _require_owner(request)
    member = (await db.execute(
        select(CompanyUser).where(
            CompanyUser.company_id == company_id, CompanyUser.user_id == user_id
        )
    )).scalar_one_or_none()
    if not member:
        raise NotFoundError("User not found in company")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found")
    from app.services.auth_service import AuthService
    auth_svc = AuthService(db)
    await auth_svc.send_password_reset(user.email)
    return {"message": "Password reset email sent"}


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_company_user(
    user_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id, _ = _require_owner(request)
    current_user_id = getattr(request.state, "user_id", None)
    if current_user_id and str(user_id) == str(current_user_id):
        raise ValidationError("Cannot remove yourself")
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(CompanyUser).where(
            CompanyUser.company_id == company_id, CompanyUser.user_id == user_id
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Contacts (T146 — US-7)
# ---------------------------------------------------------------------------

@router.get("/contacts", response_model=list[ContactOut])
async def list_contacts(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    result = await db.execute(select(Contact).where(Contact.company_id == company_id))
    return result.scalars().all()


@router.post("/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    contact = Contact(company_id=company_id, **payload.model_dump())
    db.add(contact)
    await db.commit()
    await db.refresh(contact)
    return contact


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: UUID,
    payload: ContactCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id, Contact.company_id == company_id)
    )).scalar_one_or_none()
    if not contact:
        raise NotFoundError("Contact not found")
    for field, value in payload.model_dump().items():
        setattr(contact, field, value)
    await db.commit()
    await db.refresh(contact)
    return contact


@router.delete("/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(Contact).where(Contact.id == contact_id, Contact.company_id == company_id)
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Messages (T148 — US-7)
# ---------------------------------------------------------------------------

@router.get("/messages", response_model=list[MessageOut])
async def list_messages(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    result = await db.execute(
        select(Message).where(Message.company_id == company_id).order_by(Message.created_at.desc())
    )
    return result.scalars().all()


@router.post("/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def send_message(
    payload: MessageCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    if not company_id or not user_id:
        raise ForbiddenError("Company account required")
    msg = Message(
        company_id=company_id,
        sender_id=user_id,
        subject=payload.subject,
        body=payload.body,
        parent_id=payload.parent_id,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    return msg


# ---------------------------------------------------------------------------
# Inventory report (T149 — US-7)
# ---------------------------------------------------------------------------

@router.get("/inventory-report")
async def get_inventory_report(
    warehouse_id: str | None = None,
    product_id: str | None = None,
    color: str | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Returns variant stock for customer — filtered by warehouse, product, color."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    from app.models.inventory import InventoryRecord, Warehouse
    from app.models.product import Product, ProductVariant

    q = (
        select(
            ProductVariant.id.label("variant_id"),
            ProductVariant.sku,
            ProductVariant.color,
            ProductVariant.size,
            ProductVariant.sort_order,
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            Warehouse.id.label("warehouse_id"),
            Warehouse.name.label("warehouse_name"),
            InventoryRecord.quantity,
        )
        .join(Product, Product.id == ProductVariant.product_id)
        .join(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
        .join(Warehouse, Warehouse.id == InventoryRecord.warehouse_id)
        .where(ProductVariant.status == "active")
        .where(Product.status == "active")
        .where(Warehouse.is_active.is_(True))
    )

    if warehouse_id and warehouse_id != "all":
        q = q.where(Warehouse.id == warehouse_id)
    if product_id and product_id != "all":
        q = q.where(Product.id == product_id)
    if color and color != "all":
        q = q.where(ProductVariant.color == color)

    q = q.order_by(
        Product.name,
        ProductVariant.color,
        ProductVariant.sort_order,
        ProductVariant.size,
    )

    rows = (await db.execute(q)).mappings().all()

    warehouses_result = await db.execute(
        select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.name)
    )
    warehouses = warehouses_result.scalars().all()

    products_result = await db.execute(
        select(Product.id, Product.name)
        .where(Product.status == "active")
        .order_by(Product.name)
    )
    products = products_result.all()

    colors_result = await db.execute(
        select(ProductVariant.color)
        .where(ProductVariant.status == "active")
        .where(ProductVariant.color.isnot(None))
        .distinct()
        .order_by(ProductVariant.color)
    )
    colors = [r[0] for r in colors_result.all() if r[0]]

    return {
        "items": [
            {
                "variant_id": str(r["variant_id"]),
                "sku": r["sku"],
                "product_id": str(r["product_id"]),
                "product_name": r["product_name"],
                "color": r["color"] or "—",
                "size": r["size"] or "—",
                "warehouse_id": str(r["warehouse_id"]),
                "warehouse_name": r["warehouse_name"],
                "available": int(r["quantity"]),
            }
            for r in rows
        ],
        "warehouses": [{"id": str(w.id), "name": w.name} for w in warehouses],
        "products": [{"id": str(p[0]), "name": p[1]} for p in products],
        "colors": colors,
    }


# ---------------------------------------------------------------------------
# Order Templates (T165 — US-8)
# ---------------------------------------------------------------------------

from app.models.order import OrderTemplate  # noqa: E402


@router.get("/templates")
async def list_templates(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    result = await db.execute(
        select(OrderTemplate).where(OrderTemplate.company_id == company_id)
        .order_by(OrderTemplate.created_at.desc())
    )
    templates = result.scalars().all()
    import json
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "item_count": len(json.loads(t.items)),
            "created_at": t.created_at.isoformat(),
        }
        for t in templates
    ]


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(
    template_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    from sqlalchemy import delete as sa_delete
    await db.execute(
        sa_delete(OrderTemplate).where(
            OrderTemplate.id == template_id,
            OrderTemplate.company_id == company_id,
        )
    )
    await db.commit()


@router.post("/templates/{template_id}/load")
async def load_template_to_cart(
    template_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Load a saved template into cart, applying current pricing."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    tpl = (await db.execute(
        select(OrderTemplate).where(
            OrderTemplate.id == template_id,
            OrderTemplate.company_id == company_id,
        )
    )).scalar_one_or_none()
    if not tpl:
        raise NotFoundError("Template not found")

    import json
    from decimal import Decimal
    items = json.loads(tpl.items)
    from app.services.cart_service import CartService
    svc = CartService(db)
    discount = getattr(request.state, "tier_discount_percent", Decimal("0"))
    validation = await svc.validate_sku_list(items)
    added = await svc.bulk_add_validated_items(company_id, validation["valid"], discount)
    await db.commit()
    return {
        "message": f"Loaded {added} items",
        "added": added,
        "invalid": len(validation["invalid"]),
        "insufficient_stock": len(validation["insufficient_stock"]),
    }


# ---------------------------------------------------------------------------
# RMA (T179 — US-14)
# ---------------------------------------------------------------------------

from app.models.rma import RMAItem as RMAItemModel, RMARequest  # noqa: E402
from app.schemas.order import RMACreate, RMAOut  # noqa: E402


@router.get("/rma", response_model=list[RMAOut])
async def list_my_rma(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    # Get order IDs for this company
    from app.models.order import Order
    order_ids = (await db.execute(
        select(Order.id).where(Order.company_id == company_id)
    )).scalars().all()
    result = await db.execute(
        select(RMARequest).where(RMARequest.order_id.in_(order_ids))
        .order_by(RMARequest.created_at.desc())
    )
    return result.scalars().all()


@router.post("/rma", response_model=RMAOut, status_code=status.HTTP_201_CREATED)
async def create_rma(
    payload: RMACreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    if not company_id or not user_id:
        raise ForbiddenError("Company account required")

    import random
    rma_number = f"RMA-{random.randint(100000, 999999)}"

    rma = RMARequest(
        order_id=payload.order_id,
        submitted_by_id=user_id,
        rma_number=rma_number,
        reason=payload.reason,
        notes=payload.notes,
        status="pending",
    )
    db.add(rma)
    await db.flush()

    for item in payload.items:
        db.add(RMAItemModel(
            rma_id=rma.id,
            order_item_id=item.order_item_id,
            quantity=item.quantity,
            reason=item.reason,
        ))

    await db.commit()
    await db.refresh(rma)

    # Confirm RMA submission to the customer
    try:
        from app.services.email_service import EmailService as _RMAES
        from app.core.config import settings as _rma_settings
        _rma_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if _rma_user:
            _RMAES(db).send_raw(
                to_email=_rma_user.email,
                subject=f"RMA Request Received &#8212; {rma_number}",
                body_html=(
                    '<div style="font-family:sans-serif;max-width:600px;margin:0 auto">'
                    '<div style="background:#080808;padding:24px;text-align:center">'
                    '<span style="font-size:36px;font-weight:900;color:#1A5CFF">A</span>'
                    '<span style="font-size:36px;font-weight:900;color:#E8242A">F</span>'
                    '<span style="color:#fff;font-size:14px;margin-left:8px;letter-spacing:.1em">APPARELS</span>'
                    '</div>'
                    '<div style="padding:32px;background:#fff">'
                    f'<h2 style="color:#2A2830;margin:0 0 12px">Return Request Received</h2>'
                    f'<p>Hi {_rma_user.first_name or "there"},</p>'
                    f'<p>We&#39;ve received your return/exchange request and will review it within 2&#8211;3 business days.</p>'
                    '<div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">'
                    '<p style="margin:0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">RMA Number</p>'
                    f'<p style="margin:4px 0 0;font-weight:800;font-size:18px;color:#2A2830">{rma_number}</p>'
                    '<p style="margin:12px 0 0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Reason</p>'
                    f'<p style="margin:4px 0 0;font-size:13px;color:#2A2830">{payload.reason}</p>'
                    '</div>'
                    f'<p style="margin:20px 0"><a href="{_rma_settings.FRONTEND_URL}/account/rma"'
                    ' style="background:#1A5CFF;color:#fff;padding:12px 24px;border-radius:6px;'
                    'text-decoration:none;font-weight:700;display:inline-block">View My Returns &rarr;</a></p>'
                    '<p style="color:#9ca3af;font-size:12px;margin-top:24px">Questions? Call (214)&nbsp;272-7213</p>'
                    '<p style="color:#9ca3af;font-size:12px">&#8212; AF Apparels Team</p>'
                    '</div></div>'
                ),
            )
    except Exception:
        pass

    return rma


@router.get("/rma/{rma_id}", response_model=RMAOut)
async def get_rma(rma_id: UUID, request: Request, db: AsyncSession = Depends(get_db)):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    rma = (await db.execute(select(RMARequest).where(RMARequest.id == rma_id))).scalar_one_or_none()
    if not rma:
        raise NotFoundError("RMA not found")
    return rma


# ---------------------------------------------------------------------------
# Resend Registration Emails
# ---------------------------------------------------------------------------

@router.post("/resend-registration-emails")
async def resend_registration_emails(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Resend welcome/registration emails to selected user groups and/or explicit addresses."""
    company_id = getattr(request.state, "company_id", None)
    company_role = getattr(request.state, "company_role", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    if company_role != "owner":
        raise ForbiddenError("Owner role required")

    # Verify reCAPTCHA (skip if secret key not configured — dev convenience)
    from app.core.config import settings as _settings
    recaptcha_token = payload.get("recaptcha_token")
    if _settings.RECAPTCHA_SECRET_KEY:
        if not recaptcha_token:
            raise ValidationError("reCAPTCHA verification required")
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.google.com/recaptcha/api/siteverify",
                data={
                    "secret": _settings.RECAPTCHA_SECRET_KEY,
                    "response": recaptcha_token,
                },
            )
            result = resp.json()
            if not result.get("success"):
                raise ValidationError("reCAPTCHA verification failed")

    selected_groups = payload.get("groups", [])
    to_emails = [e.strip() for e in payload.get("to", "").split(",") if e.strip()]
    cc_emails = [e.strip() for e in payload.get("cc", "").split(",") if e.strip()]
    bcc_emails = [e.strip() for e in payload.get("bcc", "").split(",") if e.strip()]

    if not selected_groups and not to_emails:
        raise ValidationError("Select at least one user group or enter a TO email")

    # Collect emails from selected groups
    group_emails: list[str] = []
    if selected_groups:
        members = (await db.execute(
            select(CompanyUser).where(
                CompanyUser.company_id == company_id,
                CompanyUser.user_group.in_(selected_groups),
            )
        )).scalars().all()
        for member in members:
            user = (await db.execute(
                select(User).where(User.id == member.user_id)
            )).scalar_one_or_none()
            if user and user.email:
                group_emails.append(user.email)

    all_to = list(set(group_emails + to_emails))
    if not all_to:
        raise ValidationError("No recipients found for selected groups")

    from app.services.email_service import EmailService as _EmailService
    email_svc = _EmailService(db)

    sent_count = 0
    for email in all_to:
        try:
            email_svc.send_raw(
                to_email=email,
                subject="Welcome to AF Apparels B2B Platform",
                body_html=(
                    "<h2>Welcome to AF Apparels!</h2>"
                    "<p>This is a reminder of your registration to the AF Apparels wholesale platform.</p>"
                    f"<p>Please visit <a href='{_settings.FRONTEND_URL}/login'>our platform</a> to login.</p>"
                    "<p>If you need help, please contact us.</p>"
                    "<p>AF Apparels Team</p>"
                ),
                cc=cc_emails or None,
                bcc=bcc_emails or None,
            )
            sent_count += 1
        except Exception:
            pass

    return {"message": f"Emails sent successfully to {sent_count} recipient(s)", "sent_count": sent_count}


# ---------------------------------------------------------------------------
# Statements (T147 — US-7)
# ---------------------------------------------------------------------------

from app.models.statement import StatementTransaction  # noqa: E402


@router.get("/statements")
async def list_statements(
    date_from: str | None = None,
    date_to: str | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Return statement transactions with running balance."""
    import uuid as _uuid
    from sqlalchemy import func as _func

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    company_uuid = _uuid.UUID(str(company_id))

    # 1. Try StatementTransaction records (wholesale checkout path)
    q = select(StatementTransaction).where(StatementTransaction.company_id == company_uuid)
    if date_from:
        q = q.where(StatementTransaction.transaction_date >= date_from)
    if date_to:
        q = q.where(StatementTransaction.transaction_date <= date_to)
    q = q.order_by(StatementTransaction.transaction_date.asc(), StatementTransaction.created_at.asc())

    result = await db.execute(q)
    transactions = result.scalars().all()

    running_balance = 0.0
    items = []

    if transactions:
        for txn in transactions:
            if txn.transaction_type == "charge":
                running_balance += float(txn.amount)
            else:
                running_balance -= float(txn.amount)
            items.append({
                "id": str(txn.id),
                "date": txn.transaction_date,
                "description": txn.description,
                "type": txn.transaction_type,
                "amount": float(txn.amount),
                "reference": txn.reference_number,
                "order_id": str(txn.order_id) if txn.order_id else None,
                "running_balance": round(running_balance, 2),
            })
    else:
        # 2. Fallback: synthesize from Orders (retail/guest orders have no StatementTransactions)
        from app.models.order import Order as _Order
        order_q = select(_Order).where(
            _Order.company_id == company_uuid,
            _Order.status != "cancelled",
        )
        if date_from:
            order_q = order_q.where(_func.date(_Order.created_at) >= date_from)
        if date_to:
            order_q = order_q.where(_func.date(_Order.created_at) <= date_to)
        order_q = order_q.order_by(_Order.created_at.asc())
        orders = (await db.execute(order_q)).scalars().all()

        for order in orders:
            amount = round(float(order.total or 0), 2)
            running_balance = round(running_balance + amount, 2)
            items.append({
                "id": str(order.id),
                "date": order.created_at.strftime("%Y-%m-%d"),
                "description": f"Order {order.order_number}",
                "type": "charge",
                "amount": amount,
                "reference": order.order_number,
                "order_id": str(order.id),
                "running_balance": running_balance,
            })
            if order.payment_status == "paid":
                running_balance = round(running_balance - amount, 2)
                items.append({
                    "id": f"{order.id}-pmt",
                    "date": order.created_at.strftime("%Y-%m-%d"),
                    "description": f"Payment for {order.order_number}",
                    "type": "payment",
                    "amount": amount,
                    "reference": order.order_number,
                    "order_id": str(order.id),
                    "running_balance": running_balance,
                })

    total_charges = sum(i["amount"] for i in items if i["type"] == "charge")
    total_payments = sum(i["amount"] for i in items if i["type"] != "charge")

    return {
        "items": items,
        "summary": {
            "total_charges": round(total_charges, 2),
            "total_payments": round(total_payments, 2),
            "current_balance": round(total_charges - total_payments, 2),
        },
    }


@router.post("/statements/sync-qb")
async def sync_payments_from_qb(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Sync payments from QuickBooks Accounting API to statement transactions."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    from app.models.company import Company

    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()

    if not company or not company.qb_customer_id:
        return {"message": "No QuickBooks customer linked", "synced": 0}

    try:
        from app.services.quickbooks_service import QuickBooksService
        import httpx

        qb_svc = QuickBooksService()
        access_token = qb_svc.get_access_token()

        base_url = (
            "https://sandbox-quickbooks.api.intuit.com"
            if settings.QB_ENVIRONMENT == "sandbox"
            else "https://quickbooks.api.intuit.com"
        )
        query = f"SELECT * FROM Payment WHERE CustomerRef = '{company.qb_customer_id}' MAXRESULTS 100"

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base_url}/v3/company/{settings.QB_COMPANY_ID}/query",
                params={"query": query, "minorversion": "65"},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )

        if resp.status_code != 200:
            return {"message": f"QB sync failed (HTTP {resp.status_code})", "synced": 0}

        payments = resp.json().get("QueryResponse", {}).get("Payment", [])
        synced = 0
        for payment in payments:
            qb_id = str(payment.get("Id", ""))
            existing = (await db.execute(
                select(StatementTransaction).where(
                    StatementTransaction.qb_transaction_id == qb_id,
                    StatementTransaction.company_id == company_id,
                )
            )).scalar_one_or_none()

            if not existing:
                db.add(StatementTransaction(
                    company_id=company_id,
                    transaction_date=payment.get("TxnDate", ""),
                    description="Payment Received",
                    transaction_type="payment",
                    amount=float(payment.get("TotalAmt", 0)),
                    reference_number=payment.get("PaymentRefNum") or None,
                    qb_transaction_id=qb_id,
                ))
                synced += 1

        await db.commit()
        return {"message": f"Synced {synced} new payment(s)", "synced": synced}

    except Exception as exc:
        return {"message": f"QB sync error: {exc}", "synced": 0}


@router.get("/statements/pdf")
async def download_statement_pdf(
    date_from: str | None = None,
    date_to: str | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Download account statement as PDF."""
    import io

    from fastapi.responses import StreamingResponse

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    q = select(StatementTransaction).where(
        StatementTransaction.company_id == company_id
    )
    if date_from:
        q = q.where(StatementTransaction.transaction_date >= date_from)
    if date_to:
        q = q.where(StatementTransaction.transaction_date <= date_to)
    q = q.order_by(StatementTransaction.transaction_date.asc(), StatementTransaction.created_at.asc())

    result = await db.execute(q)
    transactions = result.scalars().all()

    from app.models.company import Company
    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    company_name = company.name if company else "AF Apparels"

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        rightMargin=0.5 * inch, leftMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("AF APPARELS", ParagraphStyle("title", fontSize=18, fontName="Helvetica-Bold")))
    story.append(Paragraph("Account Statement", ParagraphStyle("sub", fontSize=12, textColor=colors.grey)))
    story.append(Paragraph(f"Company: {company_name}", styles["Normal"]))
    if date_from or date_to:
        story.append(Paragraph(
            f"Period: {date_from or 'Beginning'} to {date_to or 'Present'}",
            styles["Normal"],
        ))
    story.append(Spacer(1, 20))

    rows = [["Date", "Description", "Reference", "Charges", "Credits", "Balance"]]
    running = 0.0
    for txn in transactions:
        if txn.transaction_type == "charge":
            running += float(txn.amount)
            charge_col = f"${float(txn.amount):,.2f}"
            credit_col = ""
        else:
            running -= float(txn.amount)
            charge_col = ""
            credit_col = f"${float(txn.amount):,.2f}"
        rows.append([
            txn.transaction_date,
            txn.description,
            txn.reference_number or "",
            charge_col,
            credit_col,
            f"${running:,.2f}",
        ])

    tbl = Table(
        rows,
        colWidths=[1.0 * inch, 2.2 * inch, 1.0 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch],
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dee2e6")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)

    story.append(Spacer(1, 20))
    total_charges = sum(float(t.amount) for t in transactions if t.transaction_type == "charge")
    total_payments = sum(float(t.amount) for t in transactions if t.transaction_type in ("payment", "credit", "refund"))
    balance = total_charges - total_payments

    summary = Table(
        [
            ["", "Total Charges:", f"${total_charges:,.2f}"],
            ["", "Total Payments:", f"${total_payments:,.2f}"],
            ["", "Current Balance:", f"${balance:,.2f}"],
        ],
        colWidths=[3.5 * inch, 1.5 * inch, 1.0 * inch],
    )
    summary.setStyle(TableStyle([
        ("FONTNAME", (1, 2), (2, 2), "Helvetica-Bold"),
        ("LINEABOVE", (1, 2), (2, 2), 1, colors.black),
        ("ALIGN", (2, 0), (2, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    story.append(summary)

    doc.build(story)

    safe_name = company_name.replace(" ", "_")
    period = date_from or "All"
    filename = f"Statement_{safe_name}_{period}.pdf"
    return StreamingResponse(
        io.BytesIO(buffer.getvalue()),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/statements/email")
async def email_statement(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Email current statement to primary contacts."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    from app.models.company import Company, Contact
    from app.services.email_service import EmailService

    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    company_name = company.name if company else "AF Apparels"

    # Prefer primary contacts; fall back to any contact
    contacts = (await db.execute(
        select(Contact).where(
            Contact.company_id == company_id,
            Contact.is_primary.is_(True),
        )
    )).scalars().all()
    if not contacts:
        contacts = (await db.execute(
            select(Contact).where(Contact.company_id == company_id)
        )).scalars().all()

    if not contacts:
        raise ValidationError("No contacts found to email statement to")

    statement_url = f"{settings.FRONTEND_URL}/account/statements"
    svc = EmailService(db)
    sent = 0
    for contact in list(contacts)[:3]:
        ok = svc.send_raw(
            to_email=contact.email,
            subject=f"Account Statement — {company_name}",
            body_html=(
                f"<h2>Account Statement</h2>"
                f"<p>Dear {contact.first_name},</p>"
                f"<p>Your latest account statement is ready.</p>"
                f"<p><a href='{statement_url}' style='background:#1d4ed8;color:#fff;"
                f"padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block'>"
                f"View Statement Online</a></p>"
                f"<p style='color:#6b7280;font-size:13px'>AF Apparels Wholesale</p>"
            ),
        )
        if ok:
            sent += 1

    return {"message": f"Statement emailed to {sent} contact(s)"}


# ---------------------------------------------------------------------------
# Abandoned Carts (T208 — customer view)
# ---------------------------------------------------------------------------

import uuid as _uuid  # noqa: E402 — avoids collision with UUID type alias above


@router.get("/abandoned-carts")
async def list_abandoned_carts(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return abandoned cart items (live CartItems inactive > 1h) for this company."""
    from datetime import datetime, timedelta, timezone
    from app.models.order import CartItem
    from app.models.product import ProductVariant, Product

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await db.execute(
        select(CartItem)
        .where(CartItem.company_id == company_id, CartItem.updated_at < cutoff)
        .order_by(CartItem.updated_at.desc())
    )
    items = result.scalars().all()

    if not items:
        return []

    details = []
    total = 0.0
    for ci in items:
        variant = (await db.execute(
            select(ProductVariant).where(ProductVariant.id == ci.variant_id)
        )).scalar_one_or_none()
        product_name = ""
        if variant:
            prod = (await db.execute(
                select(Product).where(Product.id == variant.product_id)
            )).scalar_one_or_none()
            product_name = prod.name if prod else ""
        unit = float(ci.unit_price or 0)
        line = unit * ci.quantity
        total += line
        details.append({
            "variant_id": str(ci.variant_id),
            "product_name": product_name,
            "sku": variant.sku if variant else "",
            "color": variant.color if variant else "",
            "size": variant.size if variant else "",
            "quantity": ci.quantity,
            "unit_price": unit,
            "line_total": line,
        })

    abandoned_at = max(ci.updated_at for ci in items)
    return [{
        "id": str(company_id),
        "abandoned_at": abandoned_at.isoformat(),
        "total": round(total, 2),
        "item_count": len(items),
        "items": details,
        "is_recovered": False,
    }]


@router.post("/abandoned-carts/{cart_id}/recover")
async def recover_abandoned_cart(
    cart_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Recover abandoned cart — touches cart item timestamps so they're no longer stale."""
    from datetime import timezone as _tz
    from sqlalchemy import update as sa_update
    from app.models.order import CartItem

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    now = datetime.now(_tz.utc)
    await db.execute(
        sa_update(CartItem)
        .where(CartItem.company_id == company_id)
        .values(updated_at=now)
    )
    await db.commit()
    return {"message": "Cart recovered successfully"}


@router.delete("/abandoned-carts/{cart_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_abandoned_cart(
    cart_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Clear abandoned cart items for this company."""
    from sqlalchemy import delete as sa_delete
    from app.models.order import CartItem

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    await db.execute(sa_delete(CartItem).where(CartItem.company_id == company_id))
    await db.commit()


# ---------------------------------------------------------------------------
# Price List Report — inline product prices with tier discount applied
# ---------------------------------------------------------------------------

@router.get("/price-list-report")
async def get_price_list_report(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Returns active products with variant prices (tier discount applied)."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    from decimal import Decimal
    from app.models.product import Product, ProductVariant
    from app.services.pricing_service import PricingService

    discount = Decimal(str(getattr(request.state, "tier_discount_percent", "0")))
    pricing_svc = PricingService(db)

    products_result = await db.execute(
        select(Product)
        .where(Product.status == "active")
        .order_by(Product.name)
    )
    products = products_result.scalars().all()

    items = []
    for product in products:
        variants_result = await db.execute(
            select(ProductVariant)
            .where(ProductVariant.product_id == product.id)
            .where(ProductVariant.status == "active")
            .order_by(ProductVariant.color, ProductVariant.sort_order, ProductVariant.size)
        )
        variants = variants_result.scalars().all()
        for v in variants:
            unit_price = pricing_svc.calculate_effective_price(v.retail_price, discount)
            items.append({
                "product_id": str(product.id),
                "product_name": product.name,
                "sku": v.sku,
                "color": v.color or "—",
                "size": v.size or "—",
                "retail_price": float(v.retail_price),
                "unit_price": float(unit_price),
            })

    return {"items": items, "discount_percent": float(discount)}


# ---------------------------------------------------------------------------
# Sales History Report
# ---------------------------------------------------------------------------

@router.get("/sales-history")
async def get_sales_history(
    request: Request,
    year: int = None,
    display: str = "product",
    db: AsyncSession = Depends(get_db),
):
    """Returns order sales grouped by product or showing price details for a given year."""
    import uuid as _uuid

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    company_uuid = _uuid.UUID(str(company_id))

    from app.models.order import Order, OrderItem
    from sqlalchemy import extract

    q = (
        select(OrderItem, Order.created_at, Order.order_number)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.company_id == company_uuid)
        .where(Order.status.notin_(["cancelled", "refunded"]))
    )
    if year:
        q = q.where(extract("year", Order.created_at) == year)

    q = q.order_by(OrderItem.product_name, Order.created_at)
    rows = (await db.execute(q)).all()

    if display == "price":
        result_items = [
            {
                "order_number": r[2],
                "product_name": r[0].product_name,
                "sku": r[0].sku,
                "color": r[0].color or "—",
                "size": r[0].size or "—",
                "quantity": r[0].quantity,
                "unit_price": float(r[0].unit_price),
                "line_total": float(r[0].line_total),
                "ordered_at": r[1].isoformat() if r[1] else None,
            }
            for r in rows
        ]
    else:
        from collections import defaultdict
        grouped: dict = defaultdict(lambda: {"product_name": "", "units_sold": 0, "total_revenue": 0.0, "_variants": {}})
        for r in rows:
            key = r[0].product_name
            grouped[key]["product_name"] = r[0].product_name
            grouped[key]["units_sold"] += r[0].quantity
            grouped[key]["total_revenue"] += float(r[0].line_total)
            vkey = f"{r[0].color or '—'} / {r[0].size or '—'}"
            if vkey not in grouped[key]["_variants"]:
                grouped[key]["_variants"][vkey] = {"color": r[0].color or "—", "size": r[0].size or "—", "units_sold": 0, "total_revenue": 0.0}
            grouped[key]["_variants"][vkey]["units_sold"] += r[0].quantity
            grouped[key]["_variants"][vkey]["total_revenue"] += float(r[0].line_total)
        result_items = []
        for g in grouped.values():
            variants = sorted(g.pop("_variants").values(), key=lambda v: (v["color"], v["size"]))
            result_items.append({**g, "variants": variants})

    return {"items": result_items, "year": year, "display": display}
