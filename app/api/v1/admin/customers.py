"""Admin — wholesale applications and company management."""
import logging
import uuid
from uuid import UUID

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.company import Company, CompanyUser
from app.models.order import Order
from app.models.user import User
from app.schemas.wholesale import ApproveApplicationRequest, RejectApplicationRequest, WholesaleApplicationOut
from app.schemas.company import CompanyDetail, CompanyListItem, CompanyUpdate, SuspendRequest
from app.services.wholesale_service import WholesaleService
from app.services.company_service import CompanyService
from app.types.api import PaginatedResponse


class CreateCompanyRequest(BaseModel):
    name: str
    business_type: str = "retailer"
    tax_id: str | None = None
    website: str | None = None
    phone: str | None = None
    company_email: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country: str | None = None
    # Contact person (creates/links a user account)
    contact_first_name: str | None = None
    contact_last_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    pricing_tier_id: uuid.UUID | None = None
    shipping_tier_id: uuid.UUID | None = None
    admin_notes: str | None = None
    tax_exempt: bool = False
    # Discount-group customer tag(s) — e.g. ["Tier-3"] — drives per-variant
    # tier pricing (separate from the flat-% pricing_tier).
    tags: list[str] = []
    # When true, a password-setup email is sent to the contact so the new
    # customer can set their own password and log in right away.
    send_setup_email: bool = False

router = APIRouter()


@router.get("/wholesale-applications", response_model=list[WholesaleApplicationOut])
async def list_wholesale_applications(
    status: str | None = None,
    page: int = 1,
    per_page: int = 50,
    db: AsyncSession = Depends(get_db),
) -> list[WholesaleApplicationOut]:
    service = WholesaleService(db)
    applications, _ = await service.list_applications(status=status, page=page, per_page=per_page)
    return [WholesaleApplicationOut.model_validate(a) for a in applications]


@router.post("/wholesale-applications/{application_id}/approve", status_code=200)
async def approve_application(
    application_id: uuid.UUID,
    data: ApproveApplicationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = WholesaleService(db)
    company = await service.approve(
        application_id=application_id,
        data=data,
        admin_user_id=uuid.UUID(request.state.user_id),
    )
    from app.tasks.quickbooks_tasks import sync_customer_to_qb
    from app.core.config import settings
    logger.info("Approving company %s — broker=%s", company.id, settings.CELERY_BROKER_URL)
    task = sync_customer_to_qb.delay(str(company.id))
    logger.info("QB sync task queued for company %s — task_id=%s", company.id, task.id)
    return {"message": "Application approved", "company_id": str(company.id)}


@router.post("/wholesale-applications/{application_id}/reject", status_code=200)
async def reject_application(
    application_id: uuid.UUID,
    data: RejectApplicationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    service = WholesaleService(db)
    await service.reject(
        application_id=application_id,
        data=data,
        admin_user_id=uuid.UUID(request.state.user_id),
    )
    return {"message": "Application rejected"}


# ---------------------------------------------------------------------------
# Companies (T117 — US-15)
# ---------------------------------------------------------------------------

@router.post("/companies", status_code=status.HTTP_201_CREATED)
async def create_company(
    payload: CreateCompanyRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a wholesale company account directly from admin (bypasses application flow)."""
    from app.core.security import hash_password as get_password_hash

    # Create the company
    company = Company(
        name=payload.name,
        business_type=payload.business_type,
        tax_id=payload.tax_id,
        website=payload.website,
        phone=payload.phone,
        company_email=payload.company_email,
        address_line1=payload.address_line1,
        address_line2=payload.address_line2,
        city=payload.city,
        state_province=payload.state_province,
        postal_code=payload.postal_code,
        country=payload.country or "US",
        status="active",
        pricing_tier_id=payload.pricing_tier_id,
        shipping_tier_id=payload.shipping_tier_id,
        admin_notes=payload.admin_notes,
        tax_exempt=payload.tax_exempt,
        tags=(payload.tags or None),
    )
    db.add(company)
    await db.flush()

    # Optionally create/link a user account for the contact person
    user_created = False
    if payload.contact_email:
        existing = (await db.execute(
            select(User).where(User.email == payload.contact_email)
        )).scalar_one_or_none()

        if existing:
            user = existing
        else:
            # Create a new user with a temporary password (they'll need to reset it)
            import secrets
            temp_password = secrets.token_urlsafe(16)
            user = User(
                email=payload.contact_email,
                first_name=payload.contact_first_name or "",
                last_name=payload.contact_last_name or "",
                phone=payload.contact_phone,
                hashed_password=get_password_hash(temp_password),
                is_active=True,
                email_verified=True,
            )
            db.add(user)
            await db.flush()
            user_created = True

        membership = CompanyUser(
            company_id=company.id,
            user_id=user.id,
            role="owner",
            is_active=True,
        )
        db.add(membership)

    await db.commit()

    # Optionally send the new customer a "set your password" email so they can
    # log in right away (only when we actually created their login account).
    setup_email_sent = False
    if payload.send_setup_email and user_created and payload.contact_email:
        try:
            from app.tasks.email_tasks import send_password_setup_email
            send_password_setup_email.delay(
                str(user.id),
                payload.contact_email,
                payload.contact_first_name or "there",
            )
            setup_email_sent = True
        except Exception as _e:
            logger.warning("Add-customer setup-email dispatch failed: %s", _e)

    return {
        "message": "Company created",
        "company_id": str(company.id),
        "user_created": user_created,
        "setup_email_sent": setup_email_sent,
    }


# ── Shopify customer import (T-migration) ───────────────────────────────────
# Bulk-imports customers from a Shopify "Export customers" CSV as wholesale
# companies. Accounts are created inactive with no password and NO EMAIL IS
# EVER SENT from this endpoint — admin sends a bulk activation email later,
# separately, when ready. Duplicate emails (already in our users table) are
# skipped, never overwritten.

_SHOPIFY_HEADER_ALIASES: dict[str, list[str]] = {
    "customer_id": ["customer id", "id"],
    "email": ["email"],
    "first_name": ["first name", "firstname"],
    "last_name": ["last name", "lastname"],
    "phone": ["phone", "default address phone"],
    "company": ["company", "default address company"],
    "address1": ["address1", "default address address1"],
    "address2": ["address2", "default address address2"],
    "city": ["city", "default address city"],
    "province": ["province code", "province", "default address province code", "default address province"],
    "country": ["country code", "country", "default address country code", "default address country"],
    "zip": ["zip", "default address zip"],
    "total_spent": ["total spent"],
    "total_orders": ["total orders"],
    "tags": ["tags"],
    "note": ["note"],
    "accepts_marketing": ["accepts email marketing", "accepts marketing", "email marketing consent"],
    "tax_exempt": ["tax exempt"],
}


def _normalize_csv_headers(fieldnames: list[str]) -> dict[str, list[str]]:
    """Map our logical field names → ALL matching CSV column names present (in
    alias-priority order), tolerant of Shopify's several export header variants
    (case/spacing-insensitive). Some fields are genuinely backed by two separate
    columns per row (e.g. "Phone" vs "Default Address Phone" — different rows
    populate different ones), so callers try each in order and use the first
    non-empty value for that row rather than a single column fixed for the
    whole file."""
    normalized = {(fn or "").strip().lower(): fn for fn in fieldnames}
    resolved: dict[str, list[str]] = {}
    for field, aliases in _SHOPIFY_HEADER_ALIASES.items():
        cols = [normalized[alias] for alias in aliases if alias in normalized]
        if cols:
            resolved[field] = cols
    return resolved


@router.post("/companies/import-csv")
async def import_companies_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bulk-import a Shopify customer export as wholesale companies. Silent —
    no activation or notification email is sent to anyone for any row."""
    import csv
    import io
    import secrets
    from datetime import datetime, timedelta, timezone

    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV has no header row")

    cols = _normalize_csv_headers(list(reader.fieldnames))
    if "email" not in cols:
        raise HTTPException(status_code=400, detail="CSV is missing an Email column")

    import hashlib
    from sqlalchemy import text as _sql_text

    # Discount-group customer tags (case-insensitive → exact spelling), so a CSV
    # tag "tier-3" assigns the "Tier-3" discount group.
    _grp_rows = (await db.execute(_sql_text("SELECT customer_tag FROM discount_groups WHERE customer_tag IS NOT NULL"))).all()
    group_tag_map = {(r[0] or "").strip().lower(): (r[0] or "").strip() for r in _grp_rows if (r[0] or "").strip()}

    created, updated, skipped_no_email, errors = 0, 0, 0, []

    for i, row in enumerate(reader, start=2):  # start=2: row 1 is the header
        def get(field: str) -> str:
            for col in cols.get(field, []):
                val = (row.get(col) or "").strip()
                # Excel/Shopify export prefixes long numbers and zero-padded
                # values (phones, ZIPs) with a literal apostrophe to force
                # text formatting — strip it so it doesn't end up stored as
                # part of the data.
                if val.startswith("'"):
                    val = val[1:]
                if val:
                    return val
            return ""

        email = get("email").lower()
        # No-email rows: create anyway with a deterministic placeholder email
        # (unique per Shopify Customer ID, so re-imports match instead of
        # duplicating). .invalid never resolves, so no mail can ever reach it.
        if not email:
            cid = get("customer_id") or hashlib.md5(
                f"{get('first_name')}|{get('last_name')}|{get('phone')}|{get('company')}".encode()
            ).hexdigest()[:12]
            if not cid:
                skipped_no_email += 1
                continue
            email = f"import-{cid}@afblanks-noemail.invalid"

        # Discount-group tags (tier / Stephen-5 / RAJ-6) from the CSV Tags column
        matched_tags: list[str] = []
        for t in (get("tags") or "").split(","):
            t = t.strip()
            key = t.lower()
            if t and key in group_tag_map and group_tag_map[key] not in matched_tags:
                matched_tags.append(group_tag_map[key])
        tax_exempt = get("tax_exempt").lower() in ("yes", "true", "1")

        try:
            existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if existing:
                # EXISTING customer — never touch their profile (name/address/
                # contact). Only (re)assign the discount-group tier + tax-exempt
                # from the CSV, which is the whole point of the run.
                async with db.begin_nested():
                    cu = (await db.execute(
                        select(CompanyUser).where(CompanyUser.user_id == existing.id)
                    )).scalars().first()
                    if cu:
                        company = (await db.execute(
                            select(Company).where(Company.id == cu.company_id)
                        )).scalar_one_or_none()
                        if company:
                            if matched_tags:            # only set when CSV has a tier tag
                                company.tags = matched_tags
                            company.tax_exempt = tax_exempt
                updated += 1
                continue

            # SAVEPOINT per row: a bad row rolls back only itself.
            async with db.begin_nested():
                first_name = get("first_name") or "Customer"
                last_name = get("last_name")
                company_name = get("company") or f"{first_name} {last_name}".strip() or email

                notes_parts = ["Imported from Shopify customer export."]
                if get("total_spent"):
                    notes_parts.append(f"Shopify total spent: ${get('total_spent')}")
                if get("total_orders"):
                    notes_parts.append(f"Shopify total orders: {get('total_orders')}")
                if get("tags"):
                    notes_parts.append(f"Shopify tags: {get('tags')}")
                if get("note"):
                    notes_parts.append(f"Shopify note: {get('note')}")

                company = Company(
                    name=company_name,
                    phone=get("phone") or None,
                    company_email=email if not email.endswith("@afblanks-noemail.invalid") else None,
                    address_line1=get("address1") or None,
                    address_line2=get("address2") or None,
                    city=get("city") or None,
                    state_province=get("province") or None,
                    postal_code=get("zip") or None,
                    country=get("country") or "US",
                    status="active",
                    tax_exempt=tax_exempt,
                    tags=matched_tags or None,
                    admin_notes="\n".join(notes_parts),
                )
                db.add(company)
                await db.flush()

                user = User(
                    email=email,
                    first_name=first_name,
                    last_name=last_name,
                    phone=get("phone") or None,
                    hashed_password=None,
                    is_active=False,
                    email_verified=False,
                    activation_token=secrets.token_urlsafe(32),
                    activation_token_expires=datetime.now(timezone.utc) + timedelta(days=180),
                )
                db.add(user)
                await db.flush()

                db.add(CompanyUser(company_id=company.id, user_id=user.id, role="owner", is_active=True))
            created += 1
        except Exception as exc:
            errors.append(f"Row {i} ({email}): {exc}")

    await db.commit()
    return {
        "created": created,
        "updated": updated,
        "skipped_duplicate": 0,  # existing are now updated, not skipped
        "skipped_no_email": skipped_no_email,
        "errors": errors,
    }


@router.get("/companies/export-csv")
async def export_companies_csv(
    q: str | None = None,
    status: str | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    import csv
    import io
    from fastapi.responses import StreamingResponse

    svc = CompanyService(db)
    companies, _ = await svc.list_companies_paginated(q=q, status=status, page=1, page_size=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Company", "Contact", "Email", "Phone", "Status", "Orders", "Total Spent", "Joined"])
    for c in companies:
        writer.writerow([
            c["name"], c.get("contact_name") or "", c.get("email") or "",
            c.get("phone") or "", c["status"], c["order_count"],
            str(c["total_spend"]), c["created_at"].isoformat(),
        ])
    try:
        from app.services.email_service import EmailService as _EmailSvc
        admin_user_id = getattr(request.state, "user_id", None) if request else None
        if admin_user_id:
            from sqlalchemy import select as _sel
            admin = (await db.execute(_sel(User).where(User.id == admin_user_id))).scalar_one_or_none()
            if admin and admin.email:
                filter_desc = f"status={status}" if status else "all statuses"
                if q:
                    filter_desc += f', search="{q}"'
                _EmailSvc(db).send_raw(
                    to_email=admin.email,
                    subject="Customers CSV Export Complete — AF Apparels",
                    body_html=(
                        '<div style="font-family:sans-serif;max-width:600px;margin:0 auto">'
                        '<div style="background:#080808;padding:24px;text-align:center">'
                        '<span style="font-size:36px;font-weight:900;color:#1A5CFF">A</span>'
                        '<span style="font-size:36px;font-weight:900;color:#E8242A">F</span>'
                        '<span style="color:#fff;font-size:14px;margin-left:8px;letter-spacing:.1em">APPARELS</span>'
                        '</div>'
                        '<div style="padding:32px;background:#fff">'
                        f'<h2 style="color:#2A2830;margin:0 0 12px">Export Complete</h2>'
                        f'<p>Hi {admin.first_name or "there"},</p>'
                        f'<p>Your customers CSV export has been generated successfully.</p>'
                        f'<div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">'
                        f'<p style="margin:0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Rows Exported</p>'
                        f'<p style="margin:4px 0 0;font-weight:800;font-size:24px;color:#2A2830">{len(companies)}</p>'
                        f'<p style="margin:12px 0 0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Filters</p>'
                        f'<p style="margin:4px 0 0;font-size:13px;color:#2A2830">{filter_desc}</p>'
                        f'</div>'
                        f'<p style="color:#6b7280;font-size:13px">The file was downloaded directly to your browser.</p>'
                        '<p style="color:#9ca3af;font-size:12px;margin-top:24px">Questions? Call (214)&nbsp;272-7213 or email info.afapparel@gmail.com</p>'
                        '<p style="color:#9ca3af;font-size:12px">— AF Apparels Team</p>'
                        '</div></div>'
                    ),
                )
    except Exception:
        pass
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=customers.csv"},
    )


@router.get("/companies", response_model=PaginatedResponse[CompanyListItem])
async def list_companies(
    q: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    svc = CompanyService(db)
    companies, total = await svc.list_companies_paginated(q=q, status=status, page=page, page_size=page_size)
    return PaginatedResponse(
        items=companies,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


@router.get("/companies/{company_id}", response_model=CompanyDetail)
async def get_company(company_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CompanyService(db)
    return await svc.get_company_detail(company_id)


@router.patch("/companies/{company_id}", response_model=CompanyDetail)
async def update_company(
    company_id: UUID, payload: CompanyUpdate, db: AsyncSession = Depends(get_db)
):
    svc = CompanyService(db)
    company = await svc.update_company_tiers(company_id, payload)
    await db.commit()
    return company


class _Net30Request(BaseModel):
    net30_enabled: bool


@router.patch("/companies/{company_id}/net30", status_code=status.HTTP_200_OK)
async def toggle_net30(
    company_id: UUID, payload: _Net30Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Enable or disable Net 30 payment terms for a wholesale company."""
    from sqlalchemy import select as _sel
    company = (await db.execute(_sel(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Company not found")
    if company.status != "active":
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Net 30 can only be enabled for active (approved) wholesale companies")
    company.net30_enabled = payload.net30_enabled
    await db.commit()
    return {"net30_enabled": company.net30_enabled, "company_id": str(company.id)}


class _ShippingConfigRequest(BaseModel):
    ship_courier_enabled: bool = True
    ship_pickup_enabled: bool = True
    ship_pallet_enabled: bool = False
    ship_free_enabled: bool = False
    ship_free_min: float = 500
    ship_pallet_dallas: float = 60
    ship_pallet_houston: float = 125
    ship_pallet_other: float = 275


def _shipping_config_dict(company) -> dict:
    return {
        "ship_courier_enabled": company.ship_courier_enabled,
        "ship_pickup_enabled": company.ship_pickup_enabled,
        "ship_pallet_enabled": company.ship_pallet_enabled,
        "ship_free_enabled": company.ship_free_enabled,
        "ship_free_min": float(company.ship_free_min or 0),
        "ship_pallet_dallas": float(company.ship_pallet_dallas or 0),
        "ship_pallet_houston": float(company.ship_pallet_houston or 0),
        "ship_pallet_other": float(company.ship_pallet_other or 0),
    }


@router.get("/companies/{company_id}/shipping-config", status_code=status.HTTP_200_OK)
async def get_shipping_config(company_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Per-customer shipping options (config only — checkout wiring is Phase 2)."""
    from sqlalchemy import select as _sel
    company = (await db.execute(_sel(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Company not found")
    return _shipping_config_dict(company)


@router.patch("/companies/{company_id}/shipping-config", status_code=status.HTTP_200_OK)
async def update_shipping_config(
    company_id: UUID, payload: _ShippingConfigRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    """Save a customer's 4 shipping-option toggles + pallet rates + free-ship min."""
    from sqlalchemy import select as _sel
    company = (await db.execute(_sel(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Company not found")
    company.ship_courier_enabled = payload.ship_courier_enabled
    company.ship_pickup_enabled = payload.ship_pickup_enabled
    company.ship_pallet_enabled = payload.ship_pallet_enabled
    company.ship_free_enabled = payload.ship_free_enabled
    company.ship_free_min = payload.ship_free_min
    company.ship_pallet_dallas = payload.ship_pallet_dallas
    company.ship_pallet_houston = payload.ship_pallet_houston
    company.ship_pallet_other = payload.ship_pallet_other
    await db.commit()
    return _shipping_config_dict(company)


@router.post("/companies/{company_id}/suspend", status_code=status.HTTP_200_OK)
async def suspend_company(
    company_id: UUID, payload: SuspendRequest, db: AsyncSession = Depends(get_db)
):
    svc = CompanyService(db)
    await svc.suspend(company_id, payload.reason)
    await db.commit()
    return {"message": "Company suspended"}


@router.post("/companies/{company_id}/reactivate", status_code=status.HTTP_200_OK)
async def reactivate_company(company_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = CompanyService(db)
    await svc.reactivate(company_id)
    await db.commit()
    return {"message": "Company reactivated"}


@router.delete("/companies/{company_id}", status_code=status.HTTP_200_OK)
async def delete_company(company_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Permanently delete a company and all its memberships."""
    from sqlalchemy import delete as _delete
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if not company:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Company not found")
    await db.execute(_delete(CompanyUser).where(CompanyUser.company_id == company_id))
    await db.delete(company)
    await db.commit()
    return {"message": "Company deleted"}


@router.get("/companies/{company_id}/stats")
async def get_customer_stats(company_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(
            func.count(Order.id).label("total_orders"),
            func.coalesce(func.sum(Order.total), 0).label("total_spent"),
            func.max(Order.created_at).label("last_order_date"),
        ).where(Order.company_id == company_id, Order.status.not_in(["cancelled", "refunded"]))
    )
    row = result.one()
    return {
        "total_orders": row.total_orders or 0,
        "total_spent": float(row.total_spent or 0),
        "last_order_date": row.last_order_date,
    }


# ─── Bulk "Set Your Password" invite to all active customers ──────────────────

def _password_setup_recipients():
    """Customer users (wholesale company members) who still NEED to set a password.

    Targets users with no password yet (hashed_password IS NULL) — which is exactly
    the imported / newly-added customers who can't log in until they set one. The
    old filter required is_active=True, which excluded imported users (created
    inactive) — the very people the setup email is for."""
    return (
        select(User.id, User.email, User.first_name)
        .join(CompanyUser, CompanyUser.user_id == User.id)
        .join(Company, Company.id == CompanyUser.company_id)
        .where(User.hashed_password.is_(None), CompanyUser.is_active.is_(True), Company.status == "active")
        .distinct()
    )


@router.get("/customers/password-setup-count")
async def password_setup_count(db: AsyncSession = Depends(get_db)) -> dict:
    """How many customers would receive the 'Set Your Password' email."""
    result = await db.execute(select(func.count()).select_from(_password_setup_recipients().subquery()))
    return {"count": result.scalar_one()}


@router.post("/customers/send-password-setup")
async def send_password_setup_to_all(db: AsyncSession = Depends(get_db)) -> dict:
    """Queue a branded 'Set Your Password' email to EVERY active customer.

    Does NOT send synchronously — one Celery task per recipient, so outbound
    email is paced by the worker (no burst / rate spike). Each email carries a
    unique 14-day token to the existing /reset-password page.
    """
    from app.tasks.email_tasks import send_password_setup_email

    rows = (await db.execute(_password_setup_recipients())).all()
    recipients = [r for r in rows if r[1]]  # must have an email
    if not recipients:
        raise HTTPException(status_code=422, detail="No active customers with an email to send to")
    for user_id, email, first_name in recipients:
        send_password_setup_email.delay(str(user_id), email, first_name or "there")
    return {"queued": len(recipients)}
