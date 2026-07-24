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
    return {
        "message": "Company created",
        "company_id": str(company.id),
        "user_created": user_created,
    }


# ── Shopify customer import (T-migration) ───────────────────────────────────
# Bulk-imports customers from a Shopify "Export customers" CSV as wholesale
# companies. Accounts are created inactive with no password and NO EMAIL IS
# EVER SENT from this endpoint — admin sends a bulk activation email later,
# separately, when ready. Duplicate emails (already in our users table) are
# skipped, never overwritten.

_SHOPIFY_HEADER_ALIASES: dict[str, list[str]] = {
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
}


def _normalize_csv_headers(fieldnames: list[str]) -> dict[str, str]:
    """Map our logical field names → the actual CSV column name present, tolerant
    of Shopify's several export header variants (case/spacing-insensitive)."""
    normalized = {(fn or "").strip().lower(): fn for fn in fieldnames}
    resolved: dict[str, str] = {}
    for field, aliases in _SHOPIFY_HEADER_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                resolved[field] = normalized[alias]
                break
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

    created, skipped_duplicate, skipped_no_email, errors = 0, 0, 0, []

    for i, row in enumerate(reader, start=2):  # start=2: row 1 is the header
        def get(field: str) -> str:
            col = cols.get(field)
            return (row.get(col) or "").strip() if col else ""

        email = get("email").lower()
        if not email:
            skipped_no_email += 1
            continue

        try:
            existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
            if existing:
                skipped_duplicate += 1
                continue

            first_name = get("first_name") or "Customer"
            last_name = get("last_name")
            company_name = get("company") or f"{first_name} {last_name}".strip() or email

            notes_parts = ["Imported from Shopify customer export."]
            if get("total_spent"):
                notes_parts.append(f"Shopify total spent: ${get('total_spent')}")
            if get("total_orders"):
                notes_parts.append(f"Shopify total orders: {get('total_orders')}")
            if get("accepts_marketing"):
                notes_parts.append(f"Shopify marketing consent: {get('accepts_marketing')}")
            if get("tags"):
                notes_parts.append(f"Shopify tags: {get('tags')}")
            if get("note"):
                notes_parts.append(f"Shopify note: {get('note')}")

            company = Company(
                name=company_name,
                phone=get("phone") or None,
                company_email=email,
                address_line1=get("address1") or None,
                address_line2=get("address2") or None,
                city=get("city") or None,
                state_province=get("province") or None,
                postal_code=get("zip") or None,
                country=get("country") or "US",
                status="active",
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
        "skipped_duplicate": skipped_duplicate,
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
