"""Admin — order management and RMA."""
import asyncio
import csv
import io
import json as _json
import logging
from datetime import date, datetime, timezone
from uuid import UUID

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ConflictError, NotFoundError
from app.models.company import Company
from app.services.backorder_rules import (
    check_not_mixed as _check_not_mixed,
    describe_line as _describe_line,
)
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.rma import RMAItem, RMARequest
from app.schemas.order import (
    AdminOrderDetail,
    AdminOrderListItem,
    CancelOrderRequest,
    DraftOrderCreate,
    OrderItemOut,
    OrderStatusUpdate,
    OrderUpdateRequest,
    RMACreate,
    RMAOut,
    RMAUpdateRequest,
    SendInvoicePayload,
    ShippingAddressUpdate,
)
from app.types.api import PaginatedResponse

router = APIRouter(prefix="/admin", tags=["admin-orders"])


def _af_email(content_html: str) -> str:
    """Wrap content in the AF Apparels branded email shell."""
    from app.core.config import settings as _cfg
    logo_url = _cfg.LOGO_URL or f"{_cfg.FRONTEND_URL}/Af-apparel%20logo.png"
    logo_html = (
        f'<img src="{logo_url}" alt="AF Apparels" '
        f'style="height:44px;width:auto;display:block;margin:0 auto" />'
        if logo_url else
        '<span style="font-size:28px;font-weight:900;color:#fff">AF APPARELS</span>'
    )
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
        'Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;background:#ffffff">'
        '<div style="background:#1B3A5C;padding:24px 32px;text-align:center;'
        'border-bottom:3px solid #E8242A">'
        + logo_html +
        '</div>'
        '<div style="padding:32px;background:#fff">'
        + content_html
        + '<div style="border-top:1px solid #e5e7eb;margin-top:28px;padding-top:20px">'
        '<p style="color:#9ca3af;font-size:12px;margin:0 0 4px">'
        'Questions? Call <a href="tel:2142727213" style="color:#1B3A5C;font-weight:700">'
        '+1\xa0(214)\xa0272-7213</a> or '
        '<a href="mailto:info@afblanks.com" style="color:#1B3A5C">info@afblanks.com</a></p>'
        '<p style="color:#9ca3af;font-size:12px;margin:4px 0 0">— AF Apparels Team</p>'
        '</div>'
        '</div></div>'
    )


# ---------------------------------------------------------------------------
# Email helper
# ---------------------------------------------------------------------------

async def _send_order_status_email(order: Order, new_status: str, db: AsyncSession) -> None:
    """Send order status update to the customer — all statuses, guest + wholesale."""
    import logging as _log_mod
    _log = _log_mod.getLogger(__name__)
    try:
        from app.services.email_service import EmailService
        from app.core.config import settings as _settings

        _LABEL = {
            "pending": "Order Received", "confirmed": "Order Confirmed",
            "processing": "Processing", "ready_for_pickup": "Ready for Pickup",
            "shipped": "Shipped", "delivered": "Delivered",
            "cancelled": "Cancelled", "refunded": "Refunded",
        }
        _COLOR = {
            "pending": "#f59e0b", "confirmed": "#3b82f6", "processing": "#8b5cf6",
            "ready_for_pickup": "#0891b2", "shipped": "#059669", "delivered": "#059669",
            "cancelled": "#ef4444", "refunded": "#6b7280",
        }
        label = _LABEL.get(new_status, new_status.replace("_", " ").title())
        color = _COLOR.get(new_status, "#7A7880")
        email_svc = EmailService(db)

        # ── Guest orders ─────────────────────────────────────────────────────
        if order.is_guest_order and order.guest_email:
            name = order.guest_name or "there"
            if new_status == "shipped":
                tracking_url = getattr(order, "tracking_url", None)
                tracking_block = ""
                if order.tracking_number:
                    tracking_link = (
                        f'<a href="{tracking_url}" style="color:#166534;font-weight:700">'
                        f'{order.tracking_number}</a>'
                        if tracking_url else f'<b>{order.tracking_number}</b>'
                    )
                    carrier_line = (
                        f'<p style="margin:6px 0 0;color:#166534;font-size:13px">'
                        f'Carrier: <b>{order.courier}</b>'
                        + (f' &mdash; {order.courier_service}' if order.courier_service else '')
                        + '</p>'
                    ) if order.courier else ""
                    track_btn = (
                        f'<p style="margin:14px 0 0">'
                        f'<a href="{tracking_url}" style="background:#059669;color:#fff;'
                        f'padding:10px 20px;border-radius:6px;text-decoration:none;'
                        f'font-weight:700;font-size:13px;display:inline-block">Track Your Package &rarr;</a></p>'
                    ) if tracking_url else ""
                    tracking_block = (
                        '<div style="background:#f0fdf4;border:1px solid #bbf7d0;'
                        'border-radius:8px;padding:16px;margin:16px 0">'
                        '<p style="margin:0 0 6px;font-weight:700;color:#166534;font-size:13px;'
                        'text-transform:uppercase;letter-spacing:.06em">Tracking Information</p>'
                        f'<p style="margin:0;color:#166534;font-size:14px">Tracking #: {tracking_link}</p>'
                        f'{carrier_line}'
                        f'{track_btn}'
                        '</div>'
                    )
                email_svc.send_raw(
                    to_email=order.guest_email,
                    subject=f"Your Order {order.order_number} Has Shipped!",
                    body_html=_af_email(
                        f'<h2 style="color:#059669;margin:0 0 12px">Your Order Has Shipped!</h2>'
                        f'<p>Hi {name},</p>'
                        f'<p>Great news &#8212; your AF Apparels order is on its way!</p>'
                        f'<div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">'
                        f'<p style="margin:0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Order Number</p>'
                        f'<p style="margin:4px 0 0;font-weight:800;font-size:18px;color:#2A2830">{order.order_number}</p>'
                        f'</div>'
                        f'{tracking_block}'
                        f'<p style="margin:20px 0">'
                        f'<a href="{_settings.FRONTEND_URL}/track-order"'
                        f' style="background:#E8242A;color:#fff;padding:12px 24px;border-radius:6px;'
                        f'text-decoration:none;font-weight:700;display:inline-block">'
                        f'Track Your Order &rarr;</a></p>'
                    ),
                )
            else:
                help_line = (
                    f'<p>Need help? Visit <a href="{_settings.FRONTEND_URL}/track-order"'
                    f' style="color:#1A5CFF">our order tracking page</a>.</p>'
                    if new_status in ("cancelled", "refunded") else ""
                )
                email_svc.send_raw(
                    to_email=order.guest_email,
                    subject=f"Order {order.order_number} Update &#8212; {label}",
                    body_html=_af_email(
                        f'<h2 style="color:{color};margin:0 0 12px">Order Update: {label}</h2>'
                        f'<p>Hi {name},</p>'
                        f'<p>Your order status has been updated.</p>'
                        f'<div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">'
                        f'<p style="margin:0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Order Number</p>'
                        f'<p style="margin:4px 0 0;font-weight:800;font-size:18px;color:#2A2830">{order.order_number}</p>'
                        f'<p style="margin:12px 0 0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">New Status</p>'
                        f'<p style="margin:4px 0 0;font-weight:700;color:{color}">{label}</p>'
                        f'</div>'
                        f'{help_line}'
                    ),
                )
            return

        # ── Wholesale orders ─────────────────────────────────────────────────
        from sqlalchemy import select as _select
        from app.models.user import User as _User
        from app.models.company import CompanyUser as _CompanyUser

        user_result = await db.execute(
            _select(_User)
            .join(_CompanyUser, _CompanyUser.user_id == _User.id)
            .where(_CompanyUser.company_id == order.company_id, _CompanyUser.is_active == True)
            .limit(1)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            return

        first = user.first_name or "there"
        order_url = f"{_settings.FRONTEND_URL}/account/orders/{order.id}"

        if new_status == "shipped":
            try:
                await email_svc.send(
                    trigger_event="order_shipped",
                    to_email=user.email,
                    variables={
                        "first_name": first,
                        "order_number": order.order_number,
                        "courier": order.courier or "Carrier",
                        "tracking_number": order.tracking_number or "N/A",
                    },
                )
            except Exception:
                # Template may not exist — fall back to raw
                tracking_url = getattr(order, "tracking_url", None)
                tracking_block_w = ""
                if order.tracking_number:
                    tracking_link_w = (
                        f'<a href="{tracking_url}" style="color:#166534;font-weight:700">'
                        f'{order.tracking_number}</a>'
                        if tracking_url else f'<b>{order.tracking_number}</b>'
                    )
                    carrier_line_w = (
                        f'<p style="margin:6px 0 0;color:#166534;font-size:13px">'
                        f'Carrier: <b>{order.courier}</b>'
                        + (f' &mdash; {order.courier_service}' if order.courier_service else '')
                        + '</p>'
                    ) if order.courier else ""
                    track_btn_w = (
                        f'<p style="margin:14px 0 0">'
                        f'<a href="{tracking_url}" style="background:#059669;color:#fff;'
                        f'padding:10px 20px;border-radius:6px;text-decoration:none;'
                        f'font-weight:700;font-size:13px;display:inline-block">Track Your Package &rarr;</a></p>'
                    ) if tracking_url else ""
                    tracking_block_w = (
                        '<div style="background:#f0fdf4;border:1px solid #bbf7d0;'
                        'border-radius:8px;padding:16px;margin:16px 0">'
                        '<p style="margin:0 0 6px;font-weight:700;color:#166534;font-size:13px;'
                        'text-transform:uppercase;letter-spacing:.06em">Tracking Information</p>'
                        f'<p style="margin:0;color:#166534;font-size:14px">Tracking #: {tracking_link_w}</p>'
                        f'{carrier_line_w}'
                        f'{track_btn_w}'
                        '</div>'
                    )
                email_svc.send_raw(
                    to_email=user.email,
                    subject=f"Your Order {order.order_number} Has Shipped!",
                    body_html=_af_email(
                        f'<h2 style="color:#059669;margin:0 0 12px">Your Order Has Shipped!</h2>'
                        f'<p>Hi {first},</p>'
                        f'<p>Great news &#8212; your AF Apparels order <b>{order.order_number}</b> is on its way!</p>'
                        f'<div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">'
                        f'<p style="margin:0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Order Number</p>'
                        f'<p style="margin:4px 0 0;font-weight:800;font-size:18px;color:#2A2830">{order.order_number}</p>'
                        f'</div>'
                        f'{tracking_block_w}'
                        f'<p style="margin:20px 0">'
                        f'<a href="{order_url}" style="background:#E8242A;color:#fff;padding:12px 24px;'
                        f'border-radius:6px;text-decoration:none;font-weight:700;display:inline-block">'
                        f'View Order &rarr;</a></p>'
                    ),
                )
        else:
            help_line = (
                '<p style="color:#6b7280;font-size:13px">Questions? Contact your account manager.</p>'
                if new_status in ("cancelled", "refunded") else ""
            )
            email_svc.send_raw(
                to_email=user.email,
                subject=f"Order {order.order_number} &#8212; {label}",
                body_html=_af_email(
                    f'<h2 style="color:{color};margin:0 0 12px">Order Update: {label}</h2>'
                    f'<p>Hi {first},</p>'
                    f'<p>Your order <b>{order.order_number}</b> has been updated to '
                    f'<b style="color:{color}">{label}</b>.</p>'
                    f'<p style="margin:20px 0">'
                    f'<a href="{order_url}" style="background:#1A5CFF;color:#fff;padding:12px 24px;'
                    f'border-radius:6px;text-decoration:none;font-weight:700;display:inline-block">'
                    f'View Order &rarr;</a></p>'
                    f'{help_line}'
                ),
            )

    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Order status email failed: %s", exc)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@router.post("/orders/draft", status_code=201)
async def create_draft_order(
    payload: DraftOrderCreate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an empty draft (pending) order for admin to fill in."""
    from uuid import UUID as _UUID
    from app.models.company import Company as _Company, CompanyUser as _CompanyUser

    company_id = _UUID(str(payload.company_id))

    # Verify company exists
    company = (await db.execute(select(_Company).where(_Company.id == company_id))).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Find an owner/user for placed_by_id (required FK)
    member = (await db.execute(
        select(_CompanyUser).where(_CompanyUser.company_id == company_id, _CompanyUser.is_active == True)
        .limit(1)
    )).scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=422, detail="Company has no active users — add a user first")

    # An admin-built order carries the same running number a customer's order
    # gets. It used to be "DRAFT-" plus six random digits, which read as a
    # different kind of document to anyone looking at it and broke the sequence
    # the customer sees. What makes it a draft is the is_draft flag, not the name.
    from sqlalchemy.exc import IntegrityError as _IntegrityError
    from app.services.order_service import OrderService as _OrderSvc

    order = None
    # Numbers are handed out by reading the highest one back, so two admins
    # starting an order in the same moment can pick the same one. order_number is
    # unique, so the loser of that race just takes the next number.
    for _attempt in range(5):
        order_number = await _OrderSvc(db)._generate_order_number()
        candidate = Order(
            company_id=company_id,
            placed_by_id=member.user_id,
            order_number=order_number,
            is_draft=True,
            status="pending",
            payment_status="unpaid",
            po_number=payload.po_number,
            notes=payload.notes,
            subtotal=0,
            shipping_cost=0,
            tax_amount=0,
            total=0,
        )
        db.add(candidate)
        try:
            await db.commit()
        except _IntegrityError:
            await db.rollback()
            logger.info("Draft order number %s was taken — retrying", order_number)
            continue
        order = candidate
        break

    if order is None:
        raise HTTPException(
            status_code=409,
            detail="Couldn't allocate an order number — please try again.",
        )

    await db.refresh(order)
    return {"id": str(order.id), "order_number": order.order_number}


@router.get("/orders", response_model=PaginatedResponse[AdminOrderListItem])
async def list_admin_orders(
    q: str | None = None,
    status: str | None = None,
    payment_status: str | None = None,
    company_id: str | None = None,
    guest_only: bool = Query(False, description="Show only guest orders"),
    date_from: date | None = Query(None, description="Filter orders created on or after this date"),
    date_to: date | None = Query(None, description="Filter orders created on or before this date"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import outerjoin
    # LEFT JOIN so guest orders (company_id=NULL) are included
    query = select(Order, Company.name.label("company_name")).select_from(
        outerjoin(Order, Company, Order.company_id == Company.id)
    )
    if q:
        query = query.where(
            (Order.order_number.ilike(f"%{q}%"))
            | (Order.po_number.ilike(f"%{q}%"))
            | (Order.guest_email.ilike(f"%{q}%"))
        )
    if status:
        query = query.where(Order.status == status)
    if payment_status:
        query = query.where(Order.payment_status == payment_status)
    if company_id:
        query = query.where(Order.company_id == company_id)
    if guest_only:
        query = query.where(Order.is_guest_order == True)
    if date_from:
        query = query.where(Order.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.where(Order.created_at <= datetime.combine(date_to, datetime.max.time()))

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar_one()

    result = await db.execute(
        query.order_by(Order.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    rows = result.all()

    from app.services.order_service import get_return_status_map
    ret_map = await get_return_status_map(db, [row[0].id for row in rows])

    items = []
    for row in rows:
        order, company_name = row
        item_count = (await db.execute(
            select(func.count(OrderItem.id)).where(OrderItem.order_id == order.id)
        )).scalar_one()
        items.append(AdminOrderListItem(
            return_status=ret_map.get(order.id),
            id=order.id,
            order_number=order.order_number,
            company_name=company_name,
            status=order.status,
            payment_status=order.payment_status,
            po_number=order.po_number,
            total=order.total,
            item_count=item_count,
            created_at=order.created_at,
            tracking_number=order.tracking_number,
            courier=order.courier,
            courier_service=order.courier_service,
            shipped_at=order.shipped_at,
            is_guest_order=order.is_guest_order,
            is_draft=bool(getattr(order, "is_draft", False)),
            guest_email=order.guest_email,
            guest_name=order.guest_name,
            timeline=order.timeline or [],
            in_quickbooks=bool(getattr(order, "qb_invoice_id", None)),
        ))

    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size, pages=(total + page_size - 1) // page_size)


@router.get("/orders/export-csv")
async def export_orders_csv(
    q: str | None = None,
    status: str | None = None,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import outerjoin as _outerjoin
    query = select(Order, Company.name.label("company_name")).select_from(
        _outerjoin(Order, Company, Order.company_id == Company.id)
    )
    if q:
        query = query.where(Order.order_number.ilike(f"%{q}%"))
    if status:
        query = query.where(Order.status == status)
    result = await db.execute(query.order_by(Order.created_at.desc()))
    rows = result.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Order #", "Company / Guest", "Status", "Payment", "PO Number", "Total", "Created"])
    for row in rows:
        order, company_name = row
        display_name = company_name or (f"Guest: {order.guest_email}" if order.is_guest_order else "Unknown")
        writer.writerow([
            order.order_number, display_name, order.status, order.payment_status,
            order.po_number or "", str(order.total), order.created_at.isoformat(),
        ])
    # Email the admin who triggered the export
    try:
        from app.models.user import User as _ExportUser
        from app.services.email_service import EmailService as _ExportEmailSvc
        from app.core.config import settings as _exp_settings
        admin_user_id = getattr(request.state, "user_id", None) if request else None
        if admin_user_id:
            admin = (await db.execute(select(_ExportUser).where(_ExportUser.id == admin_user_id))).scalar_one_or_none()
            if admin and admin.email:
                filter_desc = f"status={status}" if status else "all statuses"
                if q:
                    filter_desc += f', search=&ldquo;{q}&rdquo;'
                _ExportEmailSvc(db).send_raw(
                    to_email=admin.email,
                    subject="Orders CSV Export Complete &#8212; AF Apparels",
                    body_html=_af_email(
                        f'<h2 style="color:#2A2830;margin:0 0 12px">Export Complete</h2>'
                        f'<p>Hi {admin.first_name or "there"},</p>'
                        f'<p>Your orders CSV export has been generated successfully.</p>'
                        f'<div style="background:#f9fafb;border-radius:8px;padding:16px;margin:16px 0">'
                        f'<p style="margin:0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Rows Exported</p>'
                        f'<p style="margin:4px 0 0;font-weight:800;font-size:24px;color:#2A2830">{len(rows)}</p>'
                        f'<p style="margin:12px 0 0;color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:.06em">Filters</p>'
                        f'<p style="margin:4px 0 0;font-size:13px;color:#2A2830">{filter_desc}</p>'
                        f'</div>'
                        f'<p style="color:#6b7280;font-size:13px">The file was downloaded directly to your browser.</p>'
                    ),
                )
    except Exception:
        pass

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )


@router.get("/orders/{order_id}", response_model=AdminOrderDetail)
async def get_admin_order(order_id: str, db: AsyncSession = Depends(get_db)):
    import uuid as _uuid
    from sqlalchemy import outerjoin

    # Resolve by order_number for prefixed ("AF-...", "DRAFT-...") and numeric ("1008")
    # formats; fall back to UUID only when the value looks like one.
    upper = order_id.upper()
    if upper.startswith("AF-") or upper.startswith("DRAFT-"):
        where_clause = Order.order_number == upper
    elif order_id.isdigit():
        where_clause = Order.order_number == order_id
    else:
        try:
            where_clause = Order.id == _uuid.UUID(order_id)
        except ValueError:
            raise NotFoundError(f"Order {order_id} not found")

    result = await db.execute(
        select(Order, Company.name.label("company_name"))
        .select_from(outerjoin(Order, Company, Order.company_id == Company.id))
        .where(where_clause)
    )
    row = result.one_or_none()
    if not row:
        raise NotFoundError(f"Order {order_id} not found")
    order, company_name = row

    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()

    # Build variant_id → product_code mapping for display
    from app.models.product import Product as _Product, ProductVariant as _PVCode
    _variant_ids = [str(i.variant_id) for i in items if i.variant_id]
    _product_code_map: dict[str, str | None] = {}
    if _variant_ids:
        _pc_rows = await db.execute(
            select(_PVCode.id, _Product.product_code)
            .join(_Product, _Product.id == _PVCode.product_id)
            .where(_PVCode.id.in_(_variant_ids))
        )
        for _vid, _pc in _pc_rows.all():
            _product_code_map[str(_vid)] = _pc

    # Calculate shipment weight from variant weights (used to pre-fill Shippo label weight)
    _GRAMS_PER_LB = 453.592
    _DEFAULT_LBS_PER_UNIT = 0.5  # standard apparel blank fallback
    try:
        from app.models.product import ProductVariant as _PV
        variant_ids = [str(item.variant_id) for item in items if item.variant_id]
        variant_weight_g: dict[str, float] = {}
        if variant_ids:
            _vr = await db.execute(
                select(_PV.id, _PV.weight_grams).where(_PV.id.in_(variant_ids))
            )
            for _vid, _wg in _vr.all():
                if _wg:
                    variant_weight_g[str(_vid)] = float(_wg)

        total_grams = 0.0
        total_qty = sum(item.quantity for item in items)
        has_variant_weights = False
        for item in items:
            _vid_s = str(item.variant_id) if item.variant_id else None
            if _vid_s and _vid_s in variant_weight_g:
                total_grams += variant_weight_g[_vid_s] * item.quantity
                has_variant_weights = True

        if has_variant_weights and total_grams > 0:
            calculated_weight_lbs = max(round(total_grams / _GRAMS_PER_LB, 2), 0.5)
        else:
            calculated_weight_lbs = max(round(total_qty * _DEFAULT_LBS_PER_UNIT, 2), 0.5)
    except Exception:
        calculated_weight_lbs = 1.0

    # Enrich with customer contact — from placing user or guest fields
    customer_name: str | None = order.guest_name if order.is_guest_order else None
    customer_email: str | None = order.guest_email if order.is_guest_order else None
    customer_phone: str | None = order.guest_phone if order.is_guest_order else None
    if not order.is_guest_order and order.placed_by_id:
        try:
            user_result = await db.execute(select(User).where(User.id == order.placed_by_id))
            user = user_result.scalar_one_or_none()
            if user:
                customer_name = f"{user.first_name} {user.last_name}".strip() or None
                customer_email = user.email
                customer_phone = user.phone
        except Exception:
            pass

    # Parse shipping address snapshot
    shipping_address: dict | None = None
    if order.shipping_address_snapshot:
        try:
            raw = _json.loads(order.shipping_address_snapshot)
            shipping_address = {
                "full_name": raw.get("full_name") or raw.get("label"),
                "address_line1": raw.get("address_line1") or raw.get("line1"),
                "address_line2": raw.get("address_line2") or raw.get("line2"),
                "city": raw.get("city"),
                "state": raw.get("state"),
                "postal_code": raw.get("postal_code"),
                "zip_code": raw.get("postal_code"),
                "country": raw.get("country"),
            }
        except Exception:
            pass

    try:
        return AdminOrderDetail(
            id=order.id,
            order_number=order.order_number,
            status=order.status,
            payment_status=order.payment_status,
            po_number=order.po_number,
            order_notes=order.notes,
            subtotal=order.subtotal,
            shipping_cost=order.shipping_cost,
            tax_amount=order.tax_amount,
            discount_percent=getattr(order, "discount_percent", 0),
            discount_amount=getattr(order, "discount_amount", 0),
            total=order.total,
            company_id=order.company_id,
            company_name=company_name,
            tracking_number=order.tracking_number,
            tracking_url=getattr(order, "tracking_url", None),
            label_url=getattr(order, "label_url", None),
            carrier=getattr(order, "carrier", None),
            shipping_rate_id=getattr(order, "shipping_rate_id", None),
            shipping_method=getattr(order, "shipping_method", None),
            courier=order.courier,
            courier_service=order.courier_service,
            shipped_at=order.shipped_at,
            qb_invoice_id=order.qb_invoice_id,
            qb_payment_id=getattr(order, "qb_payment_id", None),
            qb_echeck_status=getattr(order, "qb_echeck_status", None),
            ach_authorized_at=getattr(order, "ach_authorized_at", None),
            ach_authorized_ip=getattr(order, "ach_authorized_ip", None),
            ach_authorization_text=getattr(order, "ach_authorization_text", None),
            created_at=order.created_at,
            updated_at=order.updated_at,
            items=[
                OrderItemOut.model_validate({
                    **{c.key: getattr(i, c.key) for c in i.__table__.columns},
                    "product_code": _product_code_map.get(str(i.variant_id)),
                })
                for i in items
            ],
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            shipping_address=shipping_address,
            is_guest_order=order.is_guest_order,
            is_draft=bool(getattr(order, "is_draft", False)),
            guest_email=order.guest_email,
            guest_name=order.guest_name,
            guest_phone=order.guest_phone,
            payment_method=getattr(order, "payment_method", None),
            ach_bank_name=getattr(order, "ach_bank_name", None),
            ach_account_holder=getattr(order, "ach_account_holder", None),
            ach_account_last4=getattr(order, "ach_account_last4", None),
            ach_account_type=getattr(order, "ach_account_type", None),
            ach_verified=getattr(order, "ach_verified", None),
            payment_terms=getattr(order, "payment_terms", None),
            invoice_sent_at=getattr(order, "invoice_sent_at", None),
            marked_paid_at=getattr(order, "marked_paid_at", None),
            marked_paid_by=getattr(order, "marked_paid_by", None),
            amount_paid=order.amount_paid,
            balance_due=order.balance_due,
            is_fully_paid=order.is_fully_paid,
            timeline=order.timeline or [],
            calculated_weight_lbs=calculated_weight_lbs,
            items_edited=bool(getattr(order, "items_edited", False)),
            convenience_fee=getattr(order, "convenience_fee", None),
            all_labels=getattr(order, "all_labels", None),
        )
    except Exception as exc:
        logger.exception("get_admin_order serialization error for order %s: %s", order_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/orders/{order_id}/verify-ach", status_code=200)
async def verify_ach_payment(order_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Confirm the bank transfer landed, and settle the order everywhere.

    This is the moment an ACH order is actually paid, so it has to do everything
    "mark as paid" does — record who settled it and when, and tell QuickBooks the
    invoice has been covered. It used to write nothing but two columns, so the
    payment never reached the books: the invoice sat open in QuickBooks while our
    own screen showed it settled, and nothing anywhere said the two disagreed.
    """
    from sqlalchemy import text as _text
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    _timeline = list(order.timeline or [])
    _timeline.append({
        "status": "paid",
        "message": f"ACH transfer verified — payment received (${float(order.total or 0):.2f})",
        "created_by": "Admin",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        await db.execute(
            _text("""
                UPDATE orders
                SET ach_verified   = true,
                    payment_status = 'paid',
                    marked_paid_at = COALESCE(marked_paid_at, now()),
                    amount_paid    = COALESCE(total, 0),
                    timeline       = CAST(:tl AS jsonb)
                WHERE id = :oid
            """),
            {"tl": _json.dumps(_timeline), "oid": str(order_id)},
        )
        await db.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # force_payment so the payment is recorded even on an order QuickBooks was
    # invoiced under net terms. Recording it twice is not a risk: the task skips
    # anything that already carries a qb_payment_id.
    if order.qb_invoice_id:
        from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
        sync_order_invoice_to_qb.delay(str(order_id), force_payment=True)
        logger.info("verify_ach_payment: QB payment sync queued for order %s", order_id)
    else:
        # No invoice yet — raise one and settle it in the same run, rather than
        # waiting for a fulfilment step that may never come.
        _ensure_qb_invoice(order, force_payment=True)

    return {"status": "verified", "order_id": str(order_id)}


async def _company_unit_price(variant, order, db) -> float:
    """Price a variant for THIS order's company — the exact tier / discount-group
    price the customer sees when logged in (VariantLevelPricingOverride >
    product-level VariantPricingOverride > tier discount), NOT the regular retail
    price. Falls back to retail for guest / company-less orders."""
    from decimal import Decimal
    discount_percent = Decimal("0")
    group_id = None
    if getattr(order, "company_id", None):
        company = (await db.execute(
            select(Company).where(Company.id == order.company_id)
        )).scalar_one_or_none()
        if company:
            if company.pricing_tier_id:
                from app.models.pricing import PricingTier
                dp = (await db.execute(
                    select(PricingTier.discount_percent).where(PricingTier.id == company.pricing_tier_id)
                )).scalar_one_or_none()
                if dp is not None:
                    discount_percent = dp
            if company.tags:
                from app.models.discount_group import DiscountGroup
                gid = (await db.execute(
                    select(DiscountGroup.id).where(
                        DiscountGroup.customer_tag.in_(company.tags),
                        DiscountGroup.status == "enabled",
                    ).limit(1)
                )).scalar_one_or_none()
                if gid:
                    group_id = str(gid)
    from app.services.order_service import OrderService
    price = await OrderService(db)._snapshot_price(variant, discount_percent, group_id)
    return float(price)


@router.post("/orders/{order_id}/items", status_code=201)
async def add_order_item(
    order_id: UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add a line item to a pending/draft order."""
    from uuid import UUID as _UUID
    from app.models.product import ProductVariant

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in ("delivered", "cancelled", "refunded"):
        raise HTTPException(status_code=422, detail="Cannot add items to a completed or cancelled order")

    _assert_no_debit_in_flight(order)

    variant_id_str = payload.get("variant_id")
    quantity = int(payload.get("quantity", 1))
    if not variant_id_str or quantity < 1:
        raise HTTPException(status_code=422, detail="variant_id and quantity required")

    variant_id = _UUID(str(variant_id_str))
    variant = (await db.execute(
        select(ProductVariant).where(ProductVariant.id == variant_id)
    )).scalar_one_or_none()
    if not variant:
        raise HTTPException(status_code=404, detail="Product variant not found")

    # Price for THIS order's company (tier / discount-group), NOT the regular
    # retail price the admin catalog shows — the customer's price is authoritative
    # so a discount-group company gets its discounted pricing on admin-built orders
    # too. (No manual-override UI today; the sent unit_price is intentionally ignored.)
    unit_price = await _company_unit_price(variant, order, db)
    line_total = unit_price * quantity

    # Fetch product info for denormalized fields
    from app.models.product import Product
    product = (await db.execute(
        select(Product).where(Product.id == variant.product_id)
    )).scalar_one_or_none()

    _backordered = await _is_short_stock(variant, quantity, db)
    await _assert_uniform_order(
        order_id,
        [{
            "label": _describe_line(
                product.name if product else None, variant.color, variant.size, variant.sku
            ),
            "backordered": _backordered,
        }],
        db,
    )

    item = OrderItem(
        order_id=order_id,
        variant_id=variant_id,
        quantity=quantity,
        is_backordered=_backordered,
        unit_price=unit_price,
        line_total=line_total,
        product_name=product.name if product else "Unknown",
        sku=variant.sku or "",
        color=variant.color,
        size=variant.size,
    )
    db.add(item)

    # Recalculate order totals
    order.subtotal = float(order.subtotal or 0) + line_total
    _recalc_order_total(order)
    try:
        order.items_edited = True
    except Exception:
        pass

    await db.commit()
    _refresh_qb_invoice(order)
    return {
        "message": "Item added",
        "item_id": str(item.id),
        "unit_price": unit_price,
        "line_total": line_total,
        "subtotal": float(order.subtotal),
        "total": float(order.total),
    }


@router.post("/orders/{order_id}/price-variants", status_code=200)
async def price_order_variants(
    order_id: UUID, payload: dict, db: AsyncSession = Depends(get_db)
) -> dict:
    """Return the order-company's price for a set of variants (one color's size
    run), so the admin add-grid shows the customer's price — not the catalog price."""
    from uuid import UUID as _UUID
    from app.models.product import ProductVariant

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    prices: dict[str, float] = {}
    for vid in (payload.get("variant_ids") or []):
        try:
            variant = (await db.execute(
                select(ProductVariant).where(ProductVariant.id == _UUID(str(vid)))
            )).scalar_one_or_none()
        except Exception:
            variant = None
        if variant:
            prices[str(vid)] = await _company_unit_price(variant, order, db)
    return {"prices": prices}


@router.post("/orders/{order_id}/items/bulk", status_code=201)
async def add_order_items_bulk(
    order_id: UUID, payload: dict, db: AsyncSession = Depends(get_db)
) -> dict:
    """Add several variants (a whole size run) to an order in one shot, each priced
    for the order's company. Body: {"items": [{"variant_id": ..., "quantity": n}]}."""
    from uuid import UUID as _UUID
    from app.models.product import Product, ProductVariant

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in ("delivered", "cancelled", "refunded"):
        raise HTTPException(status_code=422, detail="Cannot add items to a completed or cancelled order")

    _assert_no_debit_in_flight(order)

    created: list = []  # (item, product_name, variant, qty, unit_price, line_total)
    added_subtotal = 0.0
    for entry in (payload.get("items") or []):
        vid = entry.get("variant_id")
        qty = int(entry.get("quantity") or 0)
        if not vid or qty < 1:
            continue
        try:
            variant = (await db.execute(
                select(ProductVariant).where(ProductVariant.id == _UUID(str(vid)))
            )).scalar_one_or_none()
        except Exception:
            variant = None
        if not variant:
            continue
        unit_price = await _company_unit_price(variant, order, db)
        line_total = unit_price * qty
        product = (await db.execute(
            select(Product).where(Product.id == variant.product_id)
        )).scalar_one_or_none()
        item = OrderItem(
            order_id=order_id, variant_id=variant.id, quantity=qty,
            is_backordered=await _is_short_stock(variant, qty, db),
            unit_price=unit_price, line_total=line_total,
            product_name=product.name if product else "Unknown",
            sku=variant.sku or "", color=variant.color, size=variant.size,
        )
        created.append((item, product.name if product else "Unknown", variant, qty, unit_price, line_total))
        added_subtotal += line_total

    if not created:
        raise HTTPException(status_code=422, detail="No valid items to add")

    # Checked before anything is staged, so a size run that would leave the order
    # half owed is turned away whole rather than partly written.
    await _assert_uniform_order(
        order_id,
        [
            {
                "label": _describe_line(pname, var.color, var.size, var.sku),
                "backordered": bool(it.is_backordered),
            }
            for it, pname, var, _q, _up, _lt in created
        ],
        db,
    )
    for _it, *_rest in created:
        db.add(_it)

    await db.flush()  # assign item ids before the session is committed/expired
    resp_items = [{
        "item_id": str(it.id), "variant_id": str(var.id), "product_name": pname,
        "sku": var.sku or "", "color": var.color, "size": var.size,
        "quantity": qty, "unit_price": up, "line_total": lt,
    } for (it, pname, var, qty, up, lt) in created]

    order.subtotal = float(order.subtotal or 0) + added_subtotal
    _recalc_order_total(order)
    try:
        order.items_edited = True
    except Exception:
        pass
    await db.commit()
    _refresh_qb_invoice(order)
    return {"items": resp_items, "subtotal": float(order.subtotal), "total": float(order.total)}


@router.delete("/orders/{order_id}/items/{item_id}", status_code=200)
async def remove_order_item(
    order_id: UUID,
    item_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a line item from a pending/draft order."""
    item = (await db.execute(
        select(OrderItem).where(OrderItem.id == item_id, OrderItem.order_id == order_id)
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if order and order.status not in ("delivered", "cancelled", "refunded"):
        order.subtotal = max(0, float(order.subtotal or 0) - float(item.line_total or 0))
        _recalc_order_total(order)
        try:
            order.items_edited = True
        except Exception:
            pass

    await db.delete(item)
    await db.commit()
    _refresh_qb_invoice(order)
    return {"message": "Item removed"}


@router.patch("/orders/{order_id}/items/{item_id}", status_code=200)
async def update_order_item(
    order_id: UUID,
    item_id: UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Change a line item's unit price and/or quantity on an order that hasn't
    completed — so an admin can agree a special price with a customer while
    building the order. Items normally price from the company's tier/discount
    group; this is the deliberate manual override. Order totals are recalculated
    from every line so the invoice always matches what's on screen."""
    item = (await db.execute(
        select(OrderItem).where(OrderItem.id == item_id, OrderItem.order_id == order_id)
    )).scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status in ("delivered", "cancelled", "refunded"):
        raise HTTPException(status_code=422, detail="Cannot edit items on a completed or cancelled order")
    _assert_no_debit_in_flight(order)

    if "unit_price" in payload and payload["unit_price"] is not None:
        try:
            new_price = round(float(payload["unit_price"]), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="unit_price must be a number")
        if new_price < 0:
            raise HTTPException(status_code=422, detail="unit_price cannot be negative")
        item.unit_price = new_price

    if "quantity" in payload and payload["quantity"] is not None:
        try:
            new_qty = int(payload["quantity"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="quantity must be a whole number")
        if new_qty < 1:
            raise HTTPException(status_code=422, detail="quantity must be at least 1")
        item.quantity = new_qty

        # Raising the quantity can push a line past what the shelf holds, which
        # makes it a backorder it was not a moment ago. Only worth re-asking
        # while the order still has to take its stock: once inventory has been
        # deducted the shelf reads low *because of this order*, and asking again
        # would call a line owed that was filled in full.
        if not getattr(order, "inventory_deducted", False):
            from app.models.product import ProductVariant as _PVar
            _variant = (await db.execute(
                select(_PVar).where(_PVar.id == item.variant_id)
            )).scalar_one_or_none() if item.variant_id else None
            if _variant is not None:
                _now_short = await _is_short_stock(_variant, new_qty, db)
                if _now_short != bool(item.is_backordered):
                    _others = (await db.execute(
                        select(OrderItem).where(
                            OrderItem.order_id == order_id, OrderItem.id != item_id
                        )
                    )).scalars().all()
                    _check_not_mixed(
                        [
                            {
                                "label": _describe_line(o.product_name, o.color, o.size, o.sku),
                                "backordered": bool(getattr(o, "is_backordered", False)),
                            }
                            for o in _others
                        ]
                        + [{
                            "label": _describe_line(item.product_name, item.color, item.size, item.sku),
                            "backordered": _now_short,
                        }]
                    )
                    item.is_backordered = _now_short

    item.line_total = round(float(item.unit_price or 0) * int(item.quantity or 0), 2)

    # Rebuild the subtotal from all lines rather than adjusting by a delta — that
    # way the order can never drift out of step with its items.
    all_items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    order.subtotal = round(sum(float(i.line_total or 0) for i in all_items), 2)
    _recalc_order_total(order)
    try:
        order.items_edited = True
    except Exception:
        pass

    await db.commit()
    _refresh_qb_invoice(order)
    return {
        "message": "Item updated",
        "item_id": str(item.id),
        "unit_price": float(item.unit_price or 0),
        "quantity": int(item.quantity or 0),
        "line_total": float(item.line_total or 0),
        "subtotal": float(order.subtotal),
        "total": float(order.total),
    }


@router.patch("/orders/{order_id}", response_model=dict)
async def update_admin_order(
    order_id: UUID,
    payload: OrderUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    old_status = order.status
    _had_qb_invoice = bool(order.qb_invoice_id)
    _fields_set = payload.model_dump(exclude_unset=True)
    for field, value in _fields_set.items():
        setattr(order, field, value)

    # Draft orders: when shipping or tax is edited, recompute the grand total
    # (subtotal + shipping + tax) so the invoice/total stays correct.
    if "shipping_cost" in _fields_set or "tax_amount" in _fields_set:
        _recalc_order_total(order)

    if payload.status and payload.status != old_status:
        entry = {
            "status": payload.status,
            "message": f"Status changed to {payload.status.replace('_', ' ').title()}",
            "created_by": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        current = list(order.timeline or [])
        current.append(entry)
        await db.execute(
            _text("UPDATE orders SET timeline = CAST(:tl AS jsonb) WHERE id = :oid"),
            {"tl": _json.dumps(current), "oid": str(order_id)},
        )

    # Moving into fulfilment takes the stock off the shelf (once). Admin-built
    # orders never went through checkout, so this is where their stock is deducted.
    if payload.status in _FULFILLMENT_STATUSES:
        await _deduct_order_inventory(
            order, db, f"Order {order.order_number} {payload.status} — stock deducted"
        )

    # Cancelling before the goods shipped returns the stock to inventory (once).
    if payload.status == "cancelled" and old_status != "cancelled":
        await _restock_order_inventory(
            order, db, "returned", f"Order {order.order_number} cancelled — stock returned"
        )

    await db.commit()

    # An order entering fulfilment must exist in QuickBooks — revenue, COGS and the
    # stock reduction all hang off its invoice.
    if payload.status in _FULFILLMENT_STATUSES:
        _ensure_qb_invoice(order)

    # This endpoint also edits shipping cost and tax, which move the total. Once an
    # invoice exists _ensure_qb_invoice returns without doing anything, so bring the
    # existing one up to date instead of leaving QB on the old figure.
    _refresh_qb_invoice(order)

    if payload.status and payload.status != old_status:
        try:
            from app.tasks.email_tasks import (
                send_order_confirmed_email,
                send_order_processing_email,
                send_ready_for_pickup_email,
                send_order_shipped_email,
                send_order_delivered_email,
                send_order_cancelled_email,
            )
            _status_task_map = {
                "confirmed":        send_order_confirmed_email,
                "processing":       send_order_processing_email,
                "ready_for_pickup": send_ready_for_pickup_email,
                "shipped":          send_order_shipped_email,
                "delivered":        send_order_delivered_email,
                "cancelled":        send_order_cancelled_email,
            }
            _task = _status_task_map.get(payload.status)
            if _task is send_order_shipped_email:
                _task.delay(str(order.id), order.tracking_number or "")
            elif _task:
                _task.delay(str(order.id))
        except Exception as _e:
            logger.warning("Status email dispatch failed: %s", _e)

        # Cancelling here must keep QuickBooks consistent: if this order was
        # already synced as an invoice, void it so QB revenue isn't inflated by a
        # cancelled order (same behaviour as the dedicated cancel endpoint).
        if payload.status == "cancelled" and _had_qb_invoice:
            try:
                from app.tasks.quickbooks_tasks import void_order_invoice_in_qb
                void_order_invoice_in_qb.delay(str(order.id))
            except Exception as _e:
                logger.warning("QB void-invoice on status-cancel dispatch failed: %s", _e)

    return {"message": "Order updated"}


@router.patch("/orders/{order_id}/shipping-address", response_model=dict)
async def update_order_shipping_address(
    order_id: UUID,
    payload: ShippingAddressUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Correct an order's shipping address — used when Shippo/USPS rejects the
    address on file (e.g. "Address not found") and the admin needs to fix a
    typo or add a missing unit/suite number before regenerating the label.

    Fully replaces the snapshot with the canonical key set (address_line1,
    full_name, postal_code, ...) that every reader (label generation, PDF
    invoices, emails) already checks first, regardless of which legacy key
    format (line1/street1) the order was originally created with.
    """
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    snapshot = {
        "full_name": payload.full_name,
        "address_line1": payload.address_line1,
        "address_line2": payload.address_line2,
        "city": payload.city,
        "state": payload.state,
        "postal_code": payload.postal_code,
        "country": payload.country,
        "phone": payload.phone,
    }
    order.shipping_address_snapshot = _json.dumps(snapshot)
    await db.commit()

    return {
        "message": "Shipping address updated",
        "shipping_address": {
            "full_name": snapshot["full_name"],
            "address_line1": snapshot["address_line1"],
            "address_line2": snapshot["address_line2"],
            "city": snapshot["city"],
            "state": snapshot["state"],
            "postal_code": snapshot["postal_code"],
            "zip_code": snapshot["postal_code"],
            "country": snapshot["country"],
        },
    }


@router.patch("/orders/{order_id}/status", response_model=dict)
async def update_order_status(
    order_id: UUID,
    payload: OrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import text as _text
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    old_status = order.status
    _had_qb_invoice = bool(order.qb_invoice_id)
    order.status = payload.status

    if payload.tracking_number is not None:
        order.tracking_number = payload.tracking_number
    if payload.tracking_url is not None:
        order.tracking_url = payload.tracking_url
    if payload.courier is not None:
        order.courier = payload.courier
    if payload.courier_service is not None:
        order.courier_service = payload.courier_service
    if payload.shipping_cost is not None:
        order.shipping_cost = payload.shipping_cost
    if payload.status == "shipped" and not order.shipped_at:
        order.shipped_at = datetime.now(timezone.utc)

    entry = {
        "status": payload.status,
        "message": f"Status changed to {payload.status.replace('_', ' ').title()}",
        "created_by": "admin",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    current = list(order.timeline or [])
    current.append(entry)
    await db.execute(
        _text("UPDATE orders SET timeline = CAST(:tl AS jsonb) WHERE id = :oid"),
        {"tl": _json.dumps(current), "oid": str(order_id)},
    )

    # Moving into fulfilment takes the stock off the shelf (once). Admin-built
    # orders never went through checkout, so this is where their stock is deducted.
    if payload.status in _FULFILLMENT_STATUSES:
        await _deduct_order_inventory(
            order, db, f"Order {order.order_number} {payload.status} — stock deducted"
        )

    # Cancelling before the goods shipped returns the stock to inventory (once).
    if payload.status == "cancelled" and old_status != "cancelled":
        await _restock_order_inventory(
            order, db, "returned", f"Order {order.order_number} cancelled — stock returned"
        )

    await db.commit()

    # An order entering fulfilment must exist in QuickBooks — revenue, COGS and the
    # stock reduction all hang off its invoice.
    if payload.status in _FULFILLMENT_STATUSES:
        _ensure_qb_invoice(order)

    if payload.status != old_status:
        try:
            from app.tasks.email_tasks import (
                send_order_confirmed_email,
                send_order_processing_email,
                send_ready_for_pickup_email,
                send_order_shipped_email,
                send_order_delivered_email,
                send_order_cancelled_email,
            )
            _status_task_map = {
                "confirmed":        send_order_confirmed_email,
                "processing":       send_order_processing_email,
                "ready_for_pickup": send_ready_for_pickup_email,
                "shipped":          send_order_shipped_email,
                "delivered":        send_order_delivered_email,
                "cancelled":        send_order_cancelled_email,
            }
            _task = _status_task_map.get(payload.status)
            if _task is send_order_shipped_email:
                _task.delay(str(order.id), order.tracking_number or "")
            elif _task:
                _task.delay(str(order.id))
        except Exception as _e:
            logger.warning("Status email dispatch failed: %s", _e)

        # Void the QB invoice on cancel so QB revenue stays consistent (matches
        # the dedicated cancel endpoint). Idempotent + skipped if never synced.
        if payload.status == "cancelled" and _had_qb_invoice:
            try:
                from app.tasks.quickbooks_tasks import void_order_invoice_in_qb
                void_order_invoice_in_qb.delay(str(order.id))
            except Exception as _e:
                logger.warning("QB void-invoice on status-cancel dispatch failed: %s", _e)

    return {"success": True, "status": order.status}


class _LabelRequest(BaseModel):
    carrier: str  # "usps" | "ups" | "fedex"


@router.post("/orders/{order_id}/labels", status_code=200)
async def generate_shipping_label(
    order_id: UUID,
    payload: _LabelRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Generate Shippo labels for a live-rate order — one label per box based on product types/sizes."""
    from app.services.shippo_service import create_label_for_box, create_shippo_label, get_client
    from app.utils.box_calculator import calculate_boxes
    from app.models.product import ProductVariant as _PV
    from sqlalchemy import text as _text

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Load items + variant weights for box calculation
    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()

    variant_weight_g: dict[str, float] = {}
    variant_ids = [str(i.variant_id) for i in items if i.variant_id]
    if variant_ids:
        try:
            vr = await db.execute(select(_PV.id, _PV.weight_grams).where(_PV.id.in_(variant_ids)))
            for _vid, _wg in vr.all():
                if _wg:
                    variant_weight_g[str(_vid)] = float(_wg)
        except Exception:
            pass

    boxes = calculate_boxes(items, variant_weight_g, override_count=getattr(order, "manual_box_count", None))
    num_boxes = len(boxes)

    carrier_name = order.carrier or payload.carrier or ""
    service_name = order.courier_service or ""
    logger.info(
        "Box calc order %s: %d item(s) → %d box(es), carrier=%r, service=%r",
        getattr(order, "order_number", str(order_id)), len(items), num_boxes, carrier_name, service_name,
    )

    # Parse shipping address — snapshot key names differ by order source:
    #   seed/admin:   "address_line1"
    #   wholesale:    "line1"  (from _resolve_address in order_service)
    #   guest:        "line1" + "phone"
    to_address = await _ship_to_address(order, db)

    # Ensure required fields have non-empty values for Shippo label purchase
    if not to_address.get("name"):
        to_address["name"] = "Customer"
    if not to_address.get("phone"):
        to_address["phone"] = "+12145550000"  # warehouse fallback

    if not carrier_name:
        return {"success": False, "error": "No carrier associated with this order. Re-fetch rates and select one."}

    all_labels: list[dict] = []

    # Always create a fresh Shippo shipment with complete address — never reuse the
    # saved checkout rate_id, which was generated without phone and will be rejected
    # by Shippo with "rate may only be purchased if generated with complete address".
    # Run all boxes in parallel to reduce N×API-round-trip latency.
    async def _make_box_label(box):
        result = await create_label_for_box(
            to_address=to_address,
            carrier_name=carrier_name,
            service_name=service_name,
            weight_lbs=box.weight_lbs,
        )
        return box.box_number, result

    box_outcomes = await asyncio.gather(
        *[_make_box_label(box) for box in boxes],
        return_exceptions=True,
    )
    for outcome in box_outcomes:
        if isinstance(outcome, BaseException):
            return {"success": False, "error": f"Label task failed: {outcome}"}
        box_num, box_result = outcome
        if not box_result.get("success"):
            return {"success": False, "error": f"Box {box_num}: {box_result.get('error', 'Label failed')}"}
        all_labels.append({
            "box_number": box_num,
            "tracking_number": box_result["tracking_number"],
            "tracking_url": box_result.get("tracking_url", ""),
            "label_url": box_result["label_url"],
            "carrier": box_result.get("carrier", carrier_name),
            "service": box_result.get("service", service_name),
        })
    all_labels.sort(key=lambda x: x["box_number"])

    if not all_labels:
        return {"success": False, "error": "No labels generated"}

    first = all_labels[0]
    order.tracking_number = first["tracking_number"]
    order.carrier = first["carrier"]
    order.courier = first["carrier"]
    order.courier_service = first["service"]
    order.status = "shipped"
    if not order.shipped_at:
        order.shipped_at = datetime.now(timezone.utc)

    # Shipping a label moves the goods out — take the stock off the shelf (once).
    # This path sets the status directly, so it must deduct here as well.
    await _deduct_order_inventory(
        order, db, f"Order {order.order_number} shipped (label generated) — stock deducted"
    )

    all_labels_json = _json.dumps(all_labels)
    await db.execute(
        _text("UPDATE orders SET label_url=:lu, tracking_url=:tu, all_labels=:al WHERE id=:oid"),
        {
            "lu": first.get("label_url"),
            "tu": first.get("tracking_url"),
            "al": all_labels_json,
            "oid": str(order_id),
        },
    )

    tracking_summary = ", ".join(lb["tracking_number"] for lb in all_labels)
    entry = {
        "status": "shipped",
        "message": (
            f"Shippo {len(all_labels)} label(s) generated via {first['carrier']} {first['service']}"
            f" — Tracking: {tracking_summary}"
        ),
        "created_by": "admin",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    current = list(order.timeline or [])
    current.append(entry)
    await db.execute(
        _text("UPDATE orders SET timeline = CAST(:tl AS jsonb) WHERE id = :oid"),
        {"tl": _json.dumps(current), "oid": str(order_id)},
    )

    await db.commit()

    # Shipping a label puts the order into fulfilment — make sure QuickBooks has
    # its invoice (revenue, COGS and the stock reduction all follow from it).
    _ensure_qb_invoice(order)

    try:
        from app.tasks.email_tasks import send_order_shipped_email as _se
        _se.delay(str(order.id), first["tracking_number"] or "")
    except Exception as _e:
        logger.warning("Shipped email dispatch failed: %s", _e)

    from app.services.shippo_service import is_test_mode as _is_test
    return {
        "success": True,
        # A test key still produces a PDF, but it is watermarked
        # "SAMPLE - DO NOT MAIL" and no carrier will accept it. Say so here rather
        # than leaving it to be discovered when the parcel is refused.
        "test_mode": _is_test(),
        "num_boxes": len(all_labels),
        "tracking_number": first["tracking_number"],
        "tracking_url": first.get("tracking_url"),
        "label_url": first.get("label_url"),
        "carrier": first["carrier"],
        "service": first["service"],
        "labels": all_labels,
    }


class _FetchRatesRequest(BaseModel):
    weight_lbs: float = 1.0
    # Labels are bought one per box (see create_label_for_box), so a rate is
    # quoted for ONE box and the shipment costs that rate x box_count.
    box_count: int = 1


@router.post("/orders/{order_id}/fetch-rates", status_code=200)
async def fetch_order_rates(
    order_id: UUID,
    payload: _FetchRatesRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return live Shippo rates for a Standard Ground order so the admin can pick one."""
    from app.services.shippo_service import get_client, WAREHOUSE_ADDRESS
    from app.utils.box_calculator import BOX_LENGTH as _BOX_L, BOX_WIDTH as _BOX_W, BOX_HEIGHT as _BOX_H
    from shippo.models import components as _comp

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Rates are quoted against a Shippo shipment, and buying one of those rates
    # ships to the address on THAT shipment — so a wrong address here is not a
    # wrong quote, it is a wrongly addressed parcel.
    to_address = await _ship_to_address(order, db)

    weight_lbs = max(payload.weight_lbs, 0.5)
    box_count = max(1, int(payload.box_count or 1))

    # This weight is for ONE box. UPS and FedEx cap a package at 150 lbs and USPS
    # at 70, so anything above that comes back from Shippo as an empty rate list
    # with no reason given - which reads as "the carriers disappeared". Say what
    # actually happened instead.
    if weight_lbs > 150:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{weight_lbs:.2f} lbs is over the 150 lb per-package limit for UPS and FedEx "
                f"(USPS stops at 70), so no carrier will quote it. This box weight is per box, "
                f"not the shipment total - split the order into more boxes, or enter the weight "
                f"of a single box."
            ),
        )

    wh = WAREHOUSE_ADDRESS

    try:
        client = get_client()
        shipment = client.shipments.create(
            _comp.ShipmentCreateRequest(
                address_from=_comp.AddressCreateRequest(
                    name=wh["name"], street1=wh["street1"], city=wh["city"],
                    state=wh["state"], zip=wh["zip"], country=wh["country"],
                    phone=wh["phone"], email=wh["email"],
                ),
                address_to=_comp.AddressCreateRequest(
                    name=to_address["name"], street1=to_address["street1"],
                    city=to_address["city"], state=to_address["state"],
                    zip=to_address["zip"], country=to_address["country"],
                ),
                # The same carton the label is bought for. Quoting a 12x10x6 while
                # buying a 20x16x12 understated every rate on screen: five times the
                # volume is roughly 28 lbs of dimensional weight against 5, so an
                # $8 quote came back as a bill many times that. A quote has to
                # describe the parcel that actually ships.
                parcels=[_comp.ParcelCreateRequest(
                    length=_BOX_L, width=_BOX_W, height=_BOX_H,
                    distance_unit=_comp.DistanceUnitEnum.IN,
                    weight=str(round(weight_lbs, 2)),
                    mass_unit=_comp.WeightUnitEnum.LB,
                )],
                async_=False,
            )
        )
        # Shippo fills a shipment's rates in progressively, so a slow carrier is
        # simply absent from the first response - re-poll until it lands. Only
        # carriers actually connected in Shippo belong here: a disconnected one is
        # never going to appear, so waiting on it spends the entire retry budget on
        # every lookup and still returns a partial list.
        from app.core.config import settings as _cfg
        _expected = {
            c.strip().lower()
            for c in (_cfg.SHIPPO_EXPECTED_CARRIERS or "").split(",")
            if c.strip()
        }
        _attempts = 6
        _missing: set[str] = set()
        for _attempt in range(_attempts):
            _present = {(r.provider or "").lower() for r in (shipment.rates or [])}
            _missing = _expected - _present
            if not _missing or _attempt == _attempts - 1:
                if _missing:
                    logger.warning("fetch-rates: %s absent after %d attempts", _missing, _attempt + 1)
                break
            logger.info("fetch-rates attempt %d: %s missing, retrying in 1.5 s", _attempt + 1, _missing)
            await asyncio.sleep(1.5)
            try:
                updated = client.shipments.get(shipment_id=shipment.object_id)
                if updated and (updated.rates or []):
                    shipment = updated
            except Exception as _re:
                logger.warning("fetch-rates re-fetch attempt %d failed: %s", _attempt + 1, _re)
                break

        rates = []
        for rate in (shipment.rates or []):
            try:
                rates.append({
                    "rate_id": rate.object_id,
                    "carrier": rate.provider or "Unknown",
                    "service": rate.servicelevel.name if rate.servicelevel else "Standard",
                    "cost": float(rate.amount),
                    "currency": rate.currency or "USD",
                    "days": rate.estimated_days,
                })
            except Exception:
                continue
        rates.sort(key=lambda r: r["cost"])
        from app.services.shippo_service import is_test_mode as _is_test
        return {
            "rates": rates,
            "test_mode": _is_test(),
            "box_count": box_count,
            "weight_per_box_lbs": round(weight_lbs, 2),
            # Named so the panel can say a carrier is still on its way rather than
            # presenting a short list as if it were the whole market.
            "missing_carriers": sorted(_missing),
        }
    except Exception as exc:
        logger.warning("Admin fetch-rates error: %s", exc)
        return {"rates": [], "error": str(exc)}


class _ManualLabelRequest(BaseModel):
    rate_id: str | None = None    # if provided, purchase this specific Shippo rate (single-box only)
    carrier: str = ""             # carrier name (e.g. "UPS", "USPS", "FedEx")
    service: str = ""             # service name (e.g. "Ground", "Priority Mail")
    weight_lbs: float = 1.0      # total order weight — used for fallback single-box only


@router.get("/orders/{order_id}/box-summary", status_code=200)
async def get_box_summary(order_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Return how many boxes an order needs and per-box weight based on product types."""
    from app.utils.box_calculator import calculate_boxes
    from app.models.product import ProductVariant as _PV

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()

    variant_weight_g: dict[str, float] = {}
    variant_ids = [str(i.variant_id) for i in items if i.variant_id]
    if variant_ids:
        vr = await db.execute(select(_PV.id, _PV.weight_grams).where(_PV.id.in_(variant_ids)))
        for _vid, _wg in vr.all():
            if _wg:
                variant_weight_g[str(_vid)] = float(_wg)

    boxes = calculate_boxes(items, variant_weight_g, override_count=getattr(order, "manual_box_count", None))
    num_boxes = len(boxes)
    total_lbs = round(sum(b.weight_lbs for b in boxes), 2)
    per_box = round(total_lbs / num_boxes, 2) if num_boxes else 0.0

    return {
        "num_boxes": num_boxes,
        "total_weight_lbs": total_lbs,
        "weight_per_box_lbs": per_box,
        "boxes": [{"box_number": b.box_number, "weight_lbs": b.weight_lbs} for b in boxes],
        "manual_box_count": getattr(order, "manual_box_count", None),
    }


@router.patch("/orders/{order_id}/discount", status_code=200)
async def set_order_discount(order_id: UUID, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    """Set (or clear) a discount on an order that hasn't completed.

    Takes either form. Send discount_percent for "10% off", and the discount keeps
    tracking the subtotal as items are added or repriced. Send discount_amount for
    "$50 off", and it stays at that figure whatever the subtotal does — which is
    what a negotiated deduction usually means. Send 0 in either to remove it.

    A percentage is stored alongside the money it works out to, so a fixed amount
    is simply a percentage of zero with the amount set directly.
    """
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    if order.status in ("delivered", "cancelled", "refunded"):
        raise HTTPException(status_code=422, detail="Cannot change the discount on a completed or cancelled order")

    subtotal = float(order.subtotal or 0)
    has_amount = "discount_amount" in body and body.get("discount_amount") is not None

    if has_amount:
        try:
            amount = round(float(body.get("discount_amount") or 0), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="discount_amount must be a number")
        if amount < 0:
            raise HTTPException(status_code=422, detail="discount_amount cannot be negative")
        if amount > subtotal:
            raise HTTPException(
                status_code=422,
                detail=f"A ${amount:,.2f} discount is more than the ${subtotal:,.2f} of goods on this order.",
            )
        # percent 0 is what marks this as a fixed amount — _recalc_order_total only
        # recomputes the amount from a percentage when one is set.
        order.discount_percent = 0
        order.discount_amount = amount
    else:
        try:
            percent = round(float(body.get("discount_percent") or 0), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="discount_percent must be a number")
        if percent < 0 or percent > 100:
            raise HTTPException(status_code=422, detail="discount_percent must be between 0 and 100")
        order.discount_percent = percent
        order.discount_amount = round(subtotal * percent / 100, 2)
    _recalc_order_total(order)
    await db.commit()
    _refresh_qb_invoice(order)

    return {
        "discount_percent": float(order.discount_percent or 0),
        "discount_amount": float(order.discount_amount or 0),
        "subtotal": float(order.subtotal or 0),
        "total": float(order.total or 0),
    }


@router.patch("/orders/{order_id}/box-count", status_code=200)
async def set_box_count(order_id: UUID, body: dict, db: AsyncSession = Depends(get_db)) -> dict:
    """Manually set how many boxes this order was actually packed in — overrides
    the automatic weight-based estimate (fewer or more boxes). Pass box_count=null
    to revert to the automatic count."""
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    raw = body.get("box_count")
    if raw is None:
        order.manual_box_count = None
    else:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="box_count must be a whole number")
        if n < 1 or n > 99:
            raise HTTPException(status_code=422, detail="box_count must be between 1 and 99")
        order.manual_box_count = n
    await db.commit()
    return {"manual_box_count": order.manual_box_count}


@router.post("/orders/{order_id}/generate-label-manual", status_code=200)
async def generate_label_manual(
    order_id: UUID,
    payload: _ManualLabelRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Generate Shippo labels for a Standard Ground order — one label per box.

    Calculates how many boxes the order needs based on product types and sizes,
    then creates a separate Shippo label for each box using the admin's selected
    carrier and service.  If the order fits in a single box and a rate_id is
    provided (admin just fetched rates), that rate is purchased directly.
    """
    from sqlalchemy import text as _text2
    from app.utils.box_calculator import calculate_boxes
    from app.models.product import ProductVariant as _PV

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Load order items and variant weights for accurate box calculation
    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))
    items = items_result.scalars().all()

    variant_weight_g: dict[str, float] = {}
    variant_ids = [str(i.variant_id) for i in items if i.variant_id]
    if variant_ids:
        try:
            vr = await db.execute(select(_PV.id, _PV.weight_grams).where(_PV.id.in_(variant_ids)))
            for _vid, _wg in vr.all():
                if _wg:
                    variant_weight_g[str(_vid)] = float(_wg)
        except Exception:
            pass

    boxes = calculate_boxes(items, variant_weight_g, override_count=getattr(order, "manual_box_count", None))
    num_boxes = len(boxes)

    # Parse shipping address
    to_address = await _ship_to_address(order, db)

    all_labels: list[dict] = []

    if num_boxes == 1 and payload.rate_id:
        # Single box with a pre-fetched rate_id — purchase directly (fastest path)
        from app.services.shippo_service import get_client
        from shippo.models import components as _comp2
        try:
            client = get_client()
            txn = client.transactions.create(
                _comp2.TransactionCreateRequest(
                    rate=payload.rate_id,
                    label_file_type=_comp2.LabelFileTypeEnum.PDF,
                    async_=False,
                )
            )
            if txn.status == _comp2.TransactionStatusEnum.SUCCESS:
                all_labels = [{
                    "box_number": 1,
                    "tracking_number": txn.tracking_number,
                    "tracking_url": txn.tracking_url_provider or "",
                    "label_url": txn.label_url,
                    "carrier": payload.carrier or "",
                    "service": payload.service or "",
                }]
            else:
                msgs = " | ".join([m.text for m in (txn.messages or []) if hasattr(m, "text")])
                return {"success": False, "error": msgs or "Label creation failed"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    else:
        # Multi-box or no rate_id: create one Shippo label per box
        from app.services.shippo_service import create_label_for_box, create_label, CARRIER_TOKENS
        carrier_name = payload.carrier or ""       # e.g. "UPS", "USPS", "FedEx"
        service_name = payload.service or ""       # e.g. "Ground", "Priority Mail"

        if carrier_name and service_name:
            async def _manual_box_label(box):
                result = await create_label_for_box(
                    to_address=to_address,
                    carrier_name=carrier_name,
                    service_name=service_name,
                    weight_lbs=box.weight_lbs,
                )
                return box.box_number, result

            manual_outcomes = await asyncio.gather(
                *[_manual_box_label(box) for box in boxes],
                return_exceptions=True,
            )
            for outcome in manual_outcomes:
                if isinstance(outcome, BaseException):
                    return {"success": False, "error": f"Label task failed: {outcome}"}
                box_num, box_result = outcome
                if not box_result.get("success"):
                    return {"success": False, "error": f"Box {box_num}: {box_result.get('error', 'Label failed')}"}
                all_labels.append({
                    "box_number": box_num,
                    "tracking_number": box_result["tracking_number"],
                    "tracking_url": box_result.get("tracking_url", ""),
                    "label_url": box_result["label_url"],
                    "carrier": box_result.get("carrier", carrier_name),
                    "service": box_result.get("service", service_name),
                })
            all_labels.sort(key=lambda x: x["box_number"])
        else:
            # Absolute fallback: weight-based single label
            carrier_token = CARRIER_TOKENS.get(carrier_name.lower(), "usps_priority")
            fallback = await create_label(
                str(order_id), to_address, carrier_token, weight_oz=payload.weight_lbs * 16.0
            )
            if not fallback.get("success"):
                return fallback
            all_labels = [{
                "box_number": 1,
                "tracking_number": fallback["tracking_number"],
                "tracking_url": fallback.get("tracking_url", ""),
                "label_url": fallback["label_url"],
                "carrier": fallback.get("carrier", ""),
                "service": fallback.get("service", ""),
            }]

    if not all_labels:
        return {"success": False, "error": "No labels generated"}

    first = all_labels[0]
    order.tracking_number = first["tracking_number"]
    order.carrier = first["carrier"]
    order.courier = first["carrier"]
    order.courier_service = first["service"]
    order.status = "shipped"
    if not order.shipped_at:
        order.shipped_at = datetime.now(timezone.utc)

    # Shipping a label moves the goods out — take the stock off the shelf (once).
    # This path sets the status directly, so it must deduct here as well.
    await _deduct_order_inventory(
        order, db, f"Order {order.order_number} shipped (label generated) — stock deducted"
    )

    all_labels_json = _json.dumps(all_labels)
    await db.execute(
        _text2("UPDATE orders SET label_url=:lu, tracking_url=:tu, all_labels=:al WHERE id=:oid"),
        {
            "lu": first.get("label_url"),
            "tu": first.get("tracking_url"),
            "al": all_labels_json,
            "oid": str(order_id),
        },
    )

    tracking_summary = ", ".join(lb["tracking_number"] for lb in all_labels)
    entry = {
        "status": "shipped",
        "message": (
            f"Shippo {len(all_labels)} label(s) generated via {first['carrier']} {first['service']}"
            f" — Tracking: {tracking_summary}"
        ),
        "created_by": "admin",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    current = list(order.timeline or [])
    current.append(entry)
    await db.execute(
        _text2("UPDATE orders SET timeline = CAST(:tl AS jsonb) WHERE id = :oid"),
        {"tl": _json.dumps(current), "oid": str(order_id)},
    )

    await db.commit()

    # Shipping a label puts the order into fulfilment — make sure QuickBooks has
    # its invoice (revenue, COGS and the stock reduction all follow from it).
    _ensure_qb_invoice(order)

    try:
        from app.tasks.email_tasks import send_order_shipped_email as _se
        _se.delay(str(order.id), first["tracking_number"] or "")
    except Exception as _e:
        logger.warning("Shipped email dispatch failed: %s", _e)

    from app.services.shippo_service import is_test_mode as _is_test
    return {
        "success": True,
        # A test key still produces a PDF, but it is watermarked
        # "SAMPLE - DO NOT MAIL" and no carrier will accept it. Say so here rather
        # than leaving it to be discovered when the parcel is refused.
        "test_mode": _is_test(),
        "num_boxes": len(all_labels),
        "tracking_number": first["tracking_number"],
        "tracking_url": first.get("tracking_url"),
        "label_url": first.get("label_url"),
        "carrier": first["carrier"],
        "service": first["service"],
        "labels": all_labels,
    }


@router.post("/orders/{order_id}/cancel", response_model=dict)
async def cancel_admin_order(
    order_id: UUID,
    payload: CancelOrderRequest,
    db: AsyncSession = Depends(get_db),
):
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    _had_qb_invoice = bool(order.qb_invoice_id)
    _was_cancelled = order.status == "cancelled"
    order.status = "cancelled"
    if hasattr(order, "notes"):
        order.notes = f"Cancelled: {payload.reason}"

    # The goods never left — put the stock back (once). The other cancel paths
    # already did this; this endpoint was missing it.
    if not _was_cancelled:
        await _restock_order_inventory(
            order, db, "returned", f"Order {order.order_number} cancelled — stock returned"
        )

    await db.commit()

    try:
        from app.tasks.email_tasks import send_order_cancelled_email as _ce
        _ce.delay(str(order.id), payload.reason or "")
    except Exception as _e:
        logger.warning("Cancelled email dispatch failed: %s", _e)

    # If this order was already synced to QuickBooks as an invoice, void it so
    # QB's revenue/P&L isn't inflated by a cancelled order.
    if _had_qb_invoice:
        try:
            from app.tasks.quickbooks_tasks import void_order_invoice_in_qb
            void_order_invoice_in_qb.delay(str(order.id))
        except Exception as _e:
            logger.warning("QB void-invoice dispatch failed: %s", _e)

    return {"message": "Order cancelled"}


@router.delete("/orders/{order_id}", response_model=dict)
async def delete_admin_order(order_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Permanently delete an order — its line items and comments go with it.

    Used to clear out junk drafts / test orders so they stop showing up
    everywhere (orders list, drafts, customer lifetime value). If the order was
    already synced to QuickBooks as an invoice, that invoice is voided first so
    QB revenue stays consistent. Blocked if a return (RMA) is linked, because the
    RMA references the order — resolve the RMA first.
    """
    from sqlalchemy import delete as _sqldelete
    from app.models.order import OrderComment

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # A return/RMA points at this order with a RESTRICT foreign key — deleting the
    # order would fail at the DB. Surface a clear message instead of a 500.
    rma = (await db.execute(
        select(RMARequest.id).where(RMARequest.order_id == order_id).limit(1)
    )).first()
    if rma:
        raise HTTPException(
            status_code=422,
            detail="This order has a return (RMA) linked — delete or resolve the RMA first.",
        )

    _order_no = order.order_number
    _had_qb_invoice = bool(order.qb_invoice_id)

    # Void the QB invoice first (if any) so a deleted order never inflates QB revenue.
    if _had_qb_invoice:
        try:
            from app.tasks.quickbooks_tasks import void_order_invoice_in_qb
            void_order_invoice_in_qb.delay(str(order.id))
        except Exception as _e:
            logger.warning("QB void-invoice on delete dispatch failed: %s", _e)

    # If this order still holds stock out of inventory (real order, not yet
    # returned), put it back before the items are gone — deleting an order whose
    # goods never shipped shouldn't silently lose that stock. Draft/never-deducted
    # orders are skipped by the flag.
    await _restock_order_inventory(
        order, db, "returned", f"Order {_order_no} deleted — stock returned"
    )

    # Remove children explicitly (avoids async lazy-load of ORM cascade), then the
    # order. Other references (discounts, statements, abandoned carts) are SET NULL.
    await db.execute(_sqldelete(OrderComment).where(OrderComment.order_id == order_id))
    await db.execute(_sqldelete(OrderItem).where(OrderItem.order_id == order_id))
    await db.execute(_sqldelete(Order).where(Order.id == order_id))
    await db.commit()

    logger.info("Order %s (%s) permanently deleted by admin", order_id, _order_no)
    return {"message": f"Order {_order_no} deleted"}


@router.post("/orders/{order_id}/resend-invoice", response_model=dict)
async def resend_invoice_email(order_id: UUID, db: AsyncSession = Depends(get_db)):
    """Generate and email the invoice PDF to the customer (or admin in dev)."""
    from sqlalchemy.orm import selectinload
    from app.services.email_service import EmailService

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    # Resolve customer email
    to_email: str | None = None
    if order.is_guest_order:
        to_email = order.guest_email
    elif order.placed_by_id:
        user_row = (await db.execute(
            select(User).where(User.id == order.placed_by_id)
        )).scalar_one_or_none()
        if user_row:
            to_email = user_row.email

    if not to_email:
        raise HTTPException(status_code=422, detail="No customer email found for this order")

    ok = EmailService(db).send_invoice(order, to_email)
    if not ok:
        raise HTTPException(status_code=502, detail="Invoice email failed to send")

    return {"message": f"Invoice emailed to {to_email}"}


@router.post("/orders/{order_id}/send-invoice", response_model=dict)
async def send_invoice_email(
    order_id: UUID,
    payload: SendInvoicePayload,
    db: AsyncSession = Depends(get_db),
):
    """Send (or resend) invoice with specified payment terms to the customer."""
    from sqlalchemy.orm import selectinload
    from app.services.email_service import EmailService

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Persist payment terms
    order.payment_terms = payload.payment_terms
    await db.commit()
    await db.refresh(order)

    # Resolve customer contact
    to_email: str | None = None
    customer_name: str | None = None
    if order.is_guest_order and order.guest_email:
        to_email = order.guest_email
        customer_name = order.guest_name
    elif order.placed_by_id:
        user_row = (await db.execute(select(User).where(User.id == order.placed_by_id))).scalar_one_or_none()
        if user_row:
            to_email = user_row.email
            customer_name = f"{user_row.first_name} {user_row.last_name}".strip() or None

    if not to_email:
        raise HTTPException(status_code=422, detail="No customer email found for this order")

    ok = EmailService(db).send_invoice(order, to_email, payment_terms=payload.payment_terms, customer_name=customer_name)
    if not ok:
        raise HTTPException(status_code=502, detail="Invoice email failed to send")

    _inv_timeline = list(order.timeline or [])
    _inv_timeline.append({
        "status": "invoice_sent",
        "message": f"Invoice sent to {to_email}",
        "created_by": "Admin",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    from sqlalchemy import text as _t2
    await db.execute(
        _t2("UPDATE orders SET invoice_sent_at = now(), timeline = CAST(:tl AS jsonb) WHERE id = :oid"),
        {"tl": _json.dumps(_inv_timeline), "oid": str(order_id)},
    )
    await db.commit()

    return {"message": f"Invoice sent to {to_email}"}


@router.post("/orders/{order_id}/mark-paid", response_model=dict)
async def mark_order_paid(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Mark an order as paid and record who marked it."""
    from sqlalchemy import text as _t3

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Resolve admin name from request state
    admin_name = "Admin"
    admin_user_id = getattr(request.state, "user_id", None) if request else None
    if admin_user_id:
        _admin = (await db.execute(select(User).where(User.id == admin_user_id))).scalar_one_or_none()
        if _admin:
            admin_name = f"{_admin.first_name} {_admin.last_name}".strip() or "Admin"

    order.payment_status = "paid"

    # Write invoice tracking fields + timeline via raw SQL to avoid ORM column issues
    timeline = list(order.timeline or [])
    timeline.append({
        "status": "paid",
        "message": f"Payment received — marked as paid (${float(order.total):.2f})",
        "created_by": admin_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    await db.execute(
        _t3("""
            UPDATE orders
            SET payment_status = 'paid',
                marked_paid_at  = now(),
                marked_paid_by  = :admin,
                amount_paid     = COALESCE(total, 0),
                timeline        = CAST(:tl AS jsonb)
            WHERE id = :oid
        """),
        {"admin": admin_name, "tl": _json.dumps(timeline), "oid": str(order_id)},
    )
    await db.commit()

    # Settling an order is the point at which QuickBooks has to know about it —
    # both that it exists and that it has been paid. This used to send the
    # payment only when an invoice was already there, so an order built by hand
    # and marked paid without first being confirmed left nothing in the books at
    # all: no invoice to pay, and no later step that would have raised one.
    from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
    if order.qb_invoice_id:
        sync_order_invoice_to_qb.delay(str(order_id), force_payment=True)
        logger.info("mark_order_paid: QB payment sync queued for order %s", order_id)
    else:
        _ensure_qb_invoice(order, force_payment=True)

    return {"message": "Order marked as paid"}


@router.post("/orders/{order_id}/sync-quickbooks", response_model=dict)
async def sync_order_to_quickbooks(order_id: UUID, db: AsyncSession = Depends(get_db)):
    # Dispatch through the deduped helper: pressing this while an automatic sync
    # (or a Celery retry) is already in flight otherwise put two workers on the
    # same order, each repeating the whole QuickBooks conversation.
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    if order.qb_invoice_id:
        # An invoice existing is not the same as the order being settled in
        # QuickBooks. This used to report "already in QuickBooks" and stop there,
        # which meant an order whose invoice went over but whose payment never
        # did had no way back — the one repair path refused to run precisely
        # when it was needed. Send the payment now if it is genuinely missing.
        if order.payment_status == "paid" and not getattr(order, "qb_payment_id", None):
            from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
            sync_order_invoice_to_qb.delay(str(order_id), force_payment=True)
            logger.info("sync_order_to_quickbooks: missing payment queued for order %s", order_id)
            return {
                "message": "Recording the payment in QuickBooks — refresh in a moment.",
                "order_id": str(order_id),
                "action": "payment_queued",
            }
        return {
            "message": "Already in QuickBooks",
            "order_id": str(order_id),
            "action": "none",
        }

    _ensure_qb_invoice(order)
    return {"message": "QuickBooks sync queued", "order_id": str(order_id), "action": "invoice_queued"}


@router.post("/orders/{order_id}/reset-label", response_model=dict)
async def reset_order_label(order_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Clear an order's shipping label so a new one can be bought.

    Once a label was stored the panel showed it and hid the rate list, leaving no
    way to buy another. That matters whenever the first one cannot be used: the
    weight was wrong, the address changed, the printed sheet was ruined — or, as
    happened here, it was bought on a Shippo test key and came out watermarked
    "SAMPLE - DO NOT MAIL".

    This only forgets our copy. It does not refund the old label: Shippo refunds
    are requested from their dashboard (USPS allows it within 30 days on an unused
    label), and a test-key label cost nothing to begin with. The order's status is
    left alone — re-labelling is not un-shipping.
    """
    from sqlalchemy import text as _text

    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    previous = {
        "carrier": order.courier,
        "service": order.courier_service,
        "tracking": order.tracking_number,
    }
    if not order.tracking_number and not getattr(order, "label_url", None):
        return {"success": True, "message": "This order has no label to clear.", "previous": None}

    await db.execute(
        _text(
            "UPDATE orders SET label_url=NULL, tracking_url=NULL, all_labels=NULL,"
            " tracking_number=NULL WHERE id=:oid"
        ),
        {"oid": str(order_id)},
    )

    # Leave a trace: someone will ask later why the tracking number changed.
    entry = {
        "status": order.status,
        "message": (
            "Shipping label cleared for re-issue"
            + (f" — was {previous['carrier'] or ''} {previous['service'] or ''}".rstrip() if previous["carrier"] else "")
            + (f", tracking {previous['tracking']}" if previous["tracking"] else "")
        ),
        "created_by": "admin",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    current = list(order.timeline or [])
    current.append(entry)
    await db.execute(
        _text("UPDATE orders SET timeline = CAST(:tl AS jsonb) WHERE id = :oid"),
        {"tl": _json.dumps(current), "oid": str(order_id)},
    )
    await db.commit()
    logger.info("Label cleared for order %s (was %s)", order.order_number, previous)
    return {
        "success": True,
        "message": "Label cleared — fetch rates and generate a new one.",
        "previous": previous,
    }


@router.post("/orders/{order_id}/recreate-qb-invoice", response_model=dict)
async def recreate_qb_invoice(order_id: UUID, db: AsyncSession = Depends(get_db)):
    """Bring this order's QuickBooks invoice back in line with the order.

    Two different faults land here and they need opposite treatment. If the invoice
    was *deleted* in QB, the id we hold is stale and a fresh invoice must be raised.
    If the invoice still exists but shows the wrong amount — the order was edited
    after it was billed — clearing the id would not help: QB rejects the duplicate
    DocNumber and hands back the very same wrong invoice, so the button would
    report success and change nothing.

    So: refresh the existing invoice's lines, and only fall back to creating a new
    one when QB says the invoice is genuinely gone. The task declines to rewrite an
    invoice that already has a payment or credit memo against it.
    """
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb

    # Explicit admin action - clear any in-flight dedup marker so this is never
    # swallowed as a duplicate of an automatic sync.
    try:
        import redis as _redis_rc
        from app.core.config import settings as _cfg_rc
        _redis_rc.Redis.from_url(
            _cfg_rc.REDIS_URL or _cfg_rc.CELERY_BROKER_URL, socket_timeout=2
        ).delete(f"qb:order_sync_dispatched:{order_id}")
    except Exception:
        pass

    if getattr(order, "qb_invoice_id", None):
        sync_order_invoice_to_qb.delay(str(order_id), refresh_lines=True)
        return {
            "message": (
                "Updating the QuickBooks invoice to match this order — it will refresh shortly. "
                "If the invoice has already been paid it is left untouched and the mismatch is reported instead."
            ),
            "order_id": str(order_id),
        }

    sync_order_invoice_to_qb.delay(str(order_id))
    return {"message": "Recreating the invoice in QuickBooks — it will appear shortly and the customer will be emailed the new invoice.", "order_id": str(order_id)}


# ---------------------------------------------------------------------------
# Admin RMA management
# ---------------------------------------------------------------------------

@router.get("/rma")
async def list_admin_rma(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload

    query = select(RMARequest).options(
        selectinload(RMARequest.items).selectinload(RMAItem.order_item)
    )
    if status:
        query = query.where(RMARequest.status == status)
    result = await db.execute(query.order_by(RMARequest.created_at.desc()))
    rmas = result.scalars().all()

    return [
        {
            "id": str(r.id),
            "rma_number": r.rma_number,
            "order_id": str(r.order_id),
            "status": r.status,
            "reason": r.reason,
            "notes": r.notes,
            "admin_notes": r.admin_notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "refund_status": r.refund_status,
            "refund_amount": float(r.refund_amount) if r.refund_amount is not None else None,
            "restock_status": r.restock_status,
            "processing_error": r.processing_error,
            "items": [
                {
                    "id": str(item.id),
                    "quantity": item.quantity,
                    "reason": item.reason,
                    "product_name": item.order_item.product_name if item.order_item else None,
                    "sku": item.order_item.sku if item.order_item else None,
                    "color": item.order_item.color if item.order_item else None,
                    "size": item.order_item.size if item.order_item else None,
                    "unit_price": float(item.order_item.unit_price) if item.order_item else None,
                }
                for item in r.items
            ],
        }
        for r in rmas
    ]


async def _is_short_stock(variant, quantity: int, db: AsyncSession) -> bool:
    """True when this line is being sold for more than the shelf holds.

    An order an admin builds by hand never passed through the checkout's stock
    check, so nothing recorded that it was sold short — and the backorder queue,
    which is meant to show everything owed, silently missed every hand-built
    order. Same rule as checkout: a variant nobody tracks is not a backorder,
    only one that is genuinely short.
    """
    from app.models.inventory import InventoryRecord as _IRec
    if not getattr(variant, "allow_backorder", False):
        return False
    available, records = (await db.execute(
        select(
            func.coalesce(func.sum(_IRec.quantity), 0),
            func.count(_IRec.id),
        ).where(_IRec.variant_id == variant.id)
    )).one()
    return int(records or 0) > 0 and int(available or 0) < quantity


async def _assert_uniform_order(order_id: UUID, new_lines: list[dict], db: AsyncSession) -> None:
    """Refuse to leave an order part in stock and part on backorder.

    An order an admin builds by hand goes out of the same door as one a customer
    places, so it lives under the same rule: everything on it has to be
    shippable together. Lines already on the order are judged by the flag
    recorded when they were added — that is how they were sold, and a delivery
    arriving since does not change it.
    """
    existing = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order_id)
    )).scalars().all()
    _check_not_mixed(
        [
            {
                "label": _describe_line(i.product_name, i.color, i.size, i.sku),
                "backordered": bool(getattr(i, "is_backordered", False)),
            }
            for i in existing
        ]
        + new_lines
    )


async def _ship_to_address(order, db: AsyncSession) -> dict:
    """The address a parcel for this order must actually go to.

    Snapshots exist in three key formats depending on how the order was raised —
    "address_line1", the older "line1", and "street1" — and each reader used to
    pick its own subset. The rate lookup read address_line1/street1 only, missed
    "line1" entirely, and papered over the gap with a literal "123 Main St".
    A parcel was printed and posted to that address: the admin screen showed the
    customer's real street the whole time, because the screen reads "line1" and
    the label did not.

    So: one reader, every key, and no invented value. A missing address raises,
    because refusing to print a label is recoverable and posting a parcel to a
    made-up street is not.
    """
    addr = _json.loads(getattr(order, "shipping_address_snapshot", None) or "{}")
    out = {
        "name": addr.get("full_name") or addr.get("label") or addr.get("name") or "",
        "street1": addr.get("address_line1") or addr.get("line1") or addr.get("street1") or "",
        "street2": addr.get("address_line2") or addr.get("line2") or addr.get("street2") or "",
        "city": addr.get("city") or "",
        "state": addr.get("state") or addr.get("state_province") or "",
        "zip": addr.get("postal_code") or addr.get("zip_code") or addr.get("zip") or "",
        "country": addr.get("country") or "US",
        "phone": addr.get("phone") or "",
    }

    # The snapshot can be empty on older orders; the saved address book still has it.
    if not all([out["street1"], out["city"], out["state"], out["zip"]]):
        addr_id = getattr(order, "shipping_address_id", None)
        if addr_id:
            from app.models.company import UserAddress as _UA
            ua = (await db.execute(select(_UA).where(_UA.id == addr_id))).scalar_one_or_none()
            if ua:
                out = {
                    "name": ua.full_name or ua.label or "",
                    "street1": ua.address_line1 or "",
                    "street2": getattr(ua, "address_line2", "") or "",
                    "city": ua.city or "",
                    "state": ua.state or "",
                    "zip": ua.postal_code or "",
                    "country": ua.country or "US",
                    "phone": ua.phone or "",
                }

    missing = [k for k in ("street1", "city", "state", "zip") if not out[k]]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=(
                f"This order has no {', '.join(missing)} on file, so a label would be "
                f"printed to the wrong place. Fix the shipping address on the order first."
            ),
        )
    if not out["name"]:
        out["name"] = getattr(order, "order_number", "Customer")
    return out


def _recalc_order_total(order: Order) -> None:
    """Rebuild an order's total from its parts, honouring any admin discount.

    The discount is stored as both a percent (what the admin typed) and an amount
    (what actually comes off). Recomputing the amount from the percent here means
    the discount follows the subtotal automatically as items are added, repriced
    or removed — a 10% discount stays 10% instead of freezing at an old figure.

    A percent of zero means a fixed amount was agreed instead: that figure is left
    alone as items change, which is what "$50 off" is normally taken to mean. It is
    still capped at the subtotal so an order can never total less than its shipping.
    """
    subtotal = float(order.subtotal or 0)
    percent = float(getattr(order, "discount_percent", 0) or 0)
    if percent > 0:
        order.discount_amount = round(subtotal * percent / 100, 2)
    discount = min(float(getattr(order, "discount_amount", 0) or 0), subtotal)
    order.total = round(
        (subtotal - discount)
        + float(order.shipping_cost or 0)
        + float(order.tax_amount or 0),
        2,
    )


async def _resolve_warehouse_for_variant(variant_id: UUID, db: AsyncSession) -> UUID | None:
    """Pick a warehouse to restock into: whichever already holds this variant
    (highest quantity, mirroring the warehouse checkout deducts from), else
    fall back to any existing warehouse so a fresh InventoryRecord can be created."""
    from app.models.inventory import InventoryRecord, Warehouse

    rec = (await db.execute(
        select(InventoryRecord)
        .where(InventoryRecord.variant_id == variant_id)
        .order_by(InventoryRecord.quantity.desc())
        .limit(1)
    )).scalar_one_or_none()
    if rec:
        return rec.warehouse_id

    wh = (await db.execute(select(Warehouse.id).limit(1))).scalar_one_or_none()
    return wh


# Statuses that mean the goods are committed to the customer — the point at which
# an admin-built order's stock must come off the shelf.
_FULFILLMENT_STATUSES = ("confirmed", "processing", "ready_for_pickup", "shipped", "delivered")


_ECHECK_IN_FLIGHT = {"PENDING", "SUBMITTED", "IN_PROCESS", "PROCESSING", ""}


def _assert_no_debit_in_flight(order: Order) -> None:
    """Refuse to change what an order costs while its bank is being debited.

    A bank debit is raised for the order's total and then takes days to clear.
    Change the total in that window and the two no longer agree: the customer
    is billed one amount and charged another, with nothing to reconcile them.
    A card cannot drift this way — it is charged and done — so this is asked
    only of orders whose money is still moving.
    """
    echeck_id = getattr(order, "qb_echeck_id", None)
    if not echeck_id:
        return
    status = (getattr(order, "qb_echeck_status", None) or "").upper()
    if status in _ECHECK_IN_FLIGHT:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A bank transfer for ${float(order.total or 0):.2f} is still clearing on "
                "this order, so its total can't be changed yet. Wait until it shows as paid "
                "(about 1-5 business days), then edit and the invoice will follow."
            ),
        )


def _ensure_qb_invoice(order: Order, force_payment: bool = False) -> None:
    """Make sure an order entering fulfilment has an invoice in QuickBooks.

    Orders placed through checkout raise their invoice at payment time, but an
    admin-built order only reached QB if someone pressed "Sync to QB" or marked
    it paid — so a hand-built order could be delivered with no QB invoice at all:
    no revenue, no COGS, and (now that inventory follows the invoice rather than a
    QtyOnHand push) no stock reduction either.

    Call AFTER the commit so the worker reads the saved order. Safe to call on
    every status change: it returns immediately once an invoice exists, and a
    short-lived Redis key stops two triggers (e.g. a status change and a label)
    queueing the same sync twice.

    Pass force_payment when the money is already in hand. An order on net terms
    is invoiced unpaid by design, so the same run that raises the invoice will
    not settle it unless told to — and an order settled before it was ever
    invoiced has no later trigger that would.
    """
    if getattr(order, "qb_invoice_id", None):
        return  # already in QuickBooks — the sync task would skip creation anyway
    if float(order.subtotal or 0) <= 0:
        # An empty shell of an order (no priced lines yet) — QuickBooks would
        # reject a line-less invoice and the task would retry on a loop. It will
        # sync as soon as items are added and the status is set again.
        logger.info("Order %s has no priced items — QB invoice not queued yet", order.order_number)
        return
    try:
        import redis as _redis_sync
        from app.core.config import settings as _cfg
        _r = _redis_sync.Redis.from_url(
            _cfg.REDIS_URL or _cfg.CELERY_BROKER_URL, socket_timeout=2
        )
        if not _r.set(f"qb:order_sync_dispatched:{order.id}", "1", nx=True, ex=120):
            logger.info("QB invoice sync already dispatched for order %s — skipping", order.order_number)
            return
    except Exception as _e:
        # Redis unavailable — still dispatch; the task itself is idempotent via
        # the order's qb_invoice_id and QB's duplicate-DocNumber handling.
        logger.warning("QB invoice dedup check failed (%s) — dispatching anyway", _e)
    try:
        from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
        sync_order_invoice_to_qb.apply_async(
            args=[str(order.id)], kwargs={"force_payment": force_payment}, countdown=10
        )
        logger.info(
            "QB invoice sync queued for order %s (force_payment=%s)",
            order.order_number, force_payment,
        )
    except Exception as _e:
        logger.warning("QB invoice sync dispatch failed for order %s: %s", order.order_number, _e)


def _refresh_qb_invoice(order: Order) -> None:
    """Push an edited order's new figures onto its existing QuickBooks invoice.

    An invoice was written once and never revisited, so anything changed after it
    was raised — items added or removed, a price corrected, a discount applied,
    shipping adjusted — stayed in our books alone. QuickBooks kept billing the
    original amount, and the difference only ever surfaced by hand-comparing a P&L.

    Call AFTER the commit, and only for orders that already have an invoice; a
    brand-new one is _ensure_qb_invoice's job. The task declines to touch an
    invoice that already has a payment or credit memo against it.
    """
    if not getattr(order, "qb_invoice_id", None):
        return  # no invoice yet — nothing to bring up to date
    try:
        from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
        # Short delay so several edits in a row (a price, then a discount) settle
        # into one update instead of racing each other onto the same invoice.
        sync_order_invoice_to_qb.apply_async(
            args=[str(order.id)], kwargs={"refresh_lines": True}, countdown=15
        )
        logger.info("QB invoice refresh queued for order %s (order edited)", order.order_number)
    except Exception as _e:
        logger.warning("QB invoice refresh dispatch failed for order %s: %s", order.order_number, _e)


async def _deduct_order_inventory(order: Order, db: AsyncSession, note: str) -> None:
    """Take an order's stock off the shelf the first time it enters fulfilment.

    Orders placed through checkout already deduct at payment time (OrderService),
    but orders an admin builds by hand never pass through checkout — so without
    this their stock was never reduced, no matter how far the status advanced.
    Runs exactly once, guarded by ``order.inventory_deducted`` (which is also what
    lets a later cancel/delete restock it exactly once). Storm-safe: no per-variant
    QuickBooks push — a single batched sync is queued at the end.
    """
    if getattr(order, "inventory_deducted", False):
        return

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )).scalars().all()

    from app.services.inventory_service import InventoryService
    inv = InventoryService(db)
    deducted_ids: list[str] = []
    for it in items:
        if not it.variant_id or int(it.quantity or 0) <= 0:
            continue
        warehouse_id = await _resolve_warehouse_for_variant(it.variant_id, db)
        if not warehouse_id:
            continue
        await inv.adjust_stock_with_log(
            variant_id=it.variant_id,
            warehouse_id=warehouse_id,
            quantity_delta=-int(it.quantity),
            reason="sold",
            notes=note,
            sync_qb=False,  # QB moves this stock via the invoice/void, not a push
            # A line sold short must take the count below zero. Clamping it at
            # zero would erase the debt: stock would read "none left" whether one
            # unit was owed or a thousand, and the next delivery would look like
            # free stock rather than goods already spoken for.
            allow_negative=bool(getattr(it, "is_backordered", False)),
        )
        if str(it.variant_id) not in deducted_ids:
            deducted_ids.append(str(it.variant_id))

    if not deducted_ids:
        return  # nothing shippable on this order — leave the flag alone

    # Mark deducted so this never runs twice, and so cancel/delete restocks once.
    order.inventory_deducted = True

    # No QtyOnHand push to QuickBooks: this order's QB invoice already reduces the
    # quantity there and books the cost to COGS. Pushing our absolute count as well
    # made QB log a second, sale-less drop as an "Inventory Adjust" against its
    # default "Inventory Shrinkage" account.
    logger.info("Order %s deducted %d variant(s) locally — QB follows the invoice",
                order.order_number, len(deducted_ids))


async def _restock_order_inventory(order: Order, db: AsyncSession, reason: str, note: str) -> None:
    """Return an order's stock to the shelf when its goods never left — i.e. it
    was cancelled/deleted before shipping. Runs exactly once: guarded by
    ``order.inventory_deducted`` (drafts, already-restocked, and never-deducted
    orders are skipped), and flips the flag off after. Storm-safe: no per-variant
    QuickBooks push — a single batched sync is queued at the end.

    Caller must NOT have committed the delete yet; this reads the order's items.
    """
    if not getattr(order, "inventory_deducted", False):
        return

    items = (await db.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )).scalars().all()

    from app.services.inventory_service import InventoryService
    inv = InventoryService(db)
    restocked_ids: list[str] = []
    for it in items:
        if not it.variant_id or int(it.quantity or 0) <= 0:
            continue
        warehouse_id = await _resolve_warehouse_for_variant(it.variant_id, db)
        if not warehouse_id:
            continue
        await inv.adjust_stock_with_log(
            variant_id=it.variant_id,
            warehouse_id=warehouse_id,
            quantity_delta=int(it.quantity),
            reason=reason,
            notes=note,
            sync_qb=False,  # QB moves this stock via the invoice/void, not a push
        )
        if str(it.variant_id) not in restocked_ids:
            restocked_ids.append(str(it.variant_id))

    # Stock is back — don't let a second cancel/delete restock it again.
    order.inventory_deducted = False

    if restocked_ids:
        # No QtyOnHand push: voiding this order's QB invoice restores the quantity
        # there and reverses the COGS. Pushing our count too made QB log an extra,
        # document-less movement against its default "Inventory Shrinkage".
        logger.info("Order %s restocked %d variant(s) locally — QB follows the voided invoice",
                    order.order_number, len(restocked_ids))




@router.patch("/rma/{rma_id}", response_model=dict)
async def update_rma(
    rma_id: UUID,
    payload: RMAUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy.orm import selectinload

    rma = (await db.execute(
        select(RMARequest)
        .options(selectinload(RMARequest.items).selectinload(RMAItem.order_item))
        .where(RMARequest.id == rma_id)
    )).scalar_one_or_none()
    if not rma:
        raise NotFoundError(f"RMA {rma_id} not found")

    # Idempotency — an already-approved RMA has already been refunded and
    # restocked; re-approving would double-refund and double-restock.
    if payload.status == "approved" and rma.status == "approved":
        raise ConflictError(f"RMA {rma.rma_number} is already approved")

    if payload.admin_notes:
        rma.admin_notes = payload.admin_notes

    should_notify = True
    _dispatch_credit_memo = False

    if payload.status == "approved":
        order = (await db.execute(select(Order).where(Order.id == rma.order_id))).scalar_one_or_none()
        if not order:
            raise NotFoundError(f"Order for RMA {rma.rma_number} not found")

        # Capture plain values before any commit/rollback below — those expire
        # ORM attributes, and touching a relationship or column afterward can
        # trigger an implicit reload that isn't safe in every async context
        # (this exact pattern crashed the first live test of this endpoint).
        rma_number = rma.rma_number
        restock_items = [
            (item.order_item.variant_id, item.quantity)
            for item in rma.items
            if item.order_item and item.order_item.variant_id
        ]
        refund_amount = sum(
            float(item.order_item.unit_price) * item.quantity
            for item in rma.items
            if item.order_item
        )

        # ── Refund through QuickBooks Payments ─────────────────────────────
        # Whichever way the money came in is the way it goes back. A bank debit
        # used to fall through to "not applicable" here, which was true while
        # nothing was ever collected through it — now that money does move, a
        # return with no refund would simply keep the customer's money.
        refund_error: str | None = None
        refund_failed = False
        _echeck_id = getattr(order, "qb_echeck_id", None)
        _echeck_status = (getattr(order, "qb_echeck_status", None) or "").upper()
        if order.payment_status == "paid" and (order.qb_payment_charge_id or _echeck_id):
            try:
                from app.services.qb_payments_service import QBPaymentsService
                qb_pay = QBPaymentsService()
                if order.qb_payment_charge_id:
                    refund_resp = await asyncio.to_thread(
                        qb_pay.refund_charge, order.qb_payment_charge_id, refund_amount
                    )
                else:
                    # A debit cannot be reversed before it has cleared — about
                    # five business days. Saying so beats a bare failure that
                    # reads as if something is broken.
                    if _echeck_status not in ("SUCCEEDED", "SETTLED", "CAPTURED", "PAID"):
                        raise RuntimeError(
                            "The bank transfer hasn't cleared yet — it can't be refunded "
                            "until it does, which takes about five business days. Try again "
                            "once the order shows as paid."
                        )
                    refund_resp = await asyncio.to_thread(
                        qb_pay.refund_echeck, _echeck_id, refund_amount
                    )
                rma.refund_status = "refunded"
                rma.qb_refund_id = str(refund_resp.get("id") or "")
                rma.refund_amount = refund_amount
                # A near-full refund flips the order to "refunded" so reports/
                # statements reflect it. Partial refunds keep the order "paid" —
                # the RMA's own refund_amount is the record of what came back.
                if refund_amount >= float(order.total) - 0.01:
                    order.payment_status = "refunded"
            except Exception as exc:
                logger.error("RMA %s refund failed: %s", rma_number, exc, exc_info=True)
                rma.refund_status = "failed"
                rma.refund_amount = refund_amount
                refund_error = str(exc)
                refund_failed = True
        else:
            # Net terms, or unpaid — no money came through QuickBooks Payments,
            # so there is nothing here to send back. Any repayment is arranged
            # outside this flow.
            rma.refund_status = "not_applicable"
            rma.refund_amount = refund_amount

        # Mirror a successful card refund on the company's statement so the
        # return is visible there too. A "refund" line is informational — it
        # does NOT change the account balance (the money went back to the card,
        # not to store credit), matching how the statement endpoint treats it.
        if rma.refund_status == "refunded" and order.company_id and refund_amount > 0:
            from app.models.statement import StatementTransaction as _StmtTxn
            from datetime import date as _stmt_date
            db.add(_StmtTxn(
                company_id=order.company_id,
                transaction_date=_stmt_date.today().isoformat(),
                description=f"Refund — Return {rma_number}",
                transaction_type="refund",
                amount=refund_amount,
                reference_number=rma.qb_refund_id or rma_number,
                order_id=order.id,
            ))

        # Commit the refund outcome durably before attempting restock — a
        # restock failure below must not roll back a refund that already
        # went through.
        await db.commit()

        # ── Restock returned items ──────────────────────────────────────────
        restock_error: str | None = None
        restock_ok = False
        try:
            from app.services.inventory_service import InventoryService
            inv_svc = InventoryService(db)
            for variant_id, quantity in restock_items:
                warehouse_id = await _resolve_warehouse_for_variant(variant_id, db)
                if warehouse_id:
                    await inv_svc.adjust_stock_with_log(
                        variant_id=variant_id,
                        warehouse_id=warehouse_id,
                        quantity_delta=quantity,
                        reason="returned",
                        notes=f"RMA {rma_number} approved — item returned",
                        # The QB credit memo dispatched below restores the quantity
                        # and reverses COGS in QuickBooks. Pushing our count too
                        # made QB log a second, document-less movement against its
                        # default "Inventory Shrinkage" account.
                        sync_qb=False,
                    )
            restock_ok = True
        except Exception as exc:
            # A failed flush/insert leaves the session's transaction unusable
            # until rolled back — must happen before touching rma/order again.
            await db.rollback()
            logger.error("RMA %s restock failed: %s", rma_number, exc, exc_info=True)
            restock_error = str(exc)

        rma.restock_status = "done" if restock_ok else "failed"
        if restock_ok:
            rma.restocked_at = datetime.now(timezone.utc)
        rma.processing_error = " | ".join(filter(None, [refund_error, restock_error])) or None

        if refund_failed:
            # Don't tell the customer their return was approved (or silently
            # reject it) when the refund itself failed — leave it pending so
            # an admin sees it needs attention and can retry.
            rma.status = "pending"
            should_notify = False
        else:
            rma.status = "approved"
            _dispatch_credit_memo = True

            # Log the return on the order's timeline so the order history shows
            # what happened (approval + refund + restock), not just fulfillment.
            import json as _json_rma
            _refund_txt = (
                f"refunded ${refund_amount:.2f} to card"
                if rma.refund_status == "refunded"
                else "refund handled manually (no card charge)"
            )
            _rma_events = list(order.timeline or [])
            _rma_events.append({
                "status": "returned",
                "message": (
                    f"Return {rma_number} approved — {_refund_txt}"
                    + ("; items restocked" if restock_ok else "")
                ),
                "created_by": "Admin",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            from sqlalchemy import text as _rma_t
            await db.execute(
                _rma_t("UPDATE orders SET timeline = CAST(:tl AS jsonb) WHERE id = :oid"),
                {"tl": _json_rma.dumps(_rma_events), "oid": str(order.id)},
            )
    else:
        rma.status = payload.status

    await db.commit()

    # Create the QuickBooks Credit Memo (accounting reversal — reverses revenue,
    # restores inventory, reverses COGS in QB). Separate from the QB Payments card
    # refund already done above. Only when the approval actually went through.
    if _dispatch_credit_memo:
        try:
            from app.tasks.quickbooks_tasks import sync_rma_credit_memo_to_qb
            sync_rma_credit_memo_to_qb.delay(str(rma_id))
        except Exception as _e:
            logger.warning("QB credit-memo dispatch failed for RMA %s: %s", rma_id, _e)

    if should_notify:
        try:
            from app.tasks.email_tasks import send_rma_status_email
            send_rma_status_email.delay(str(rma_id))
        except Exception:
            pass

    return {
        "message": f"RMA {rma.status}",
        "refund_status": rma.refund_status,
        "refund_amount": float(rma.refund_amount) if rma.refund_amount is not None else None,
        "restock_status": rma.restock_status,
        "processing_error": rma.processing_error,
    }


# ---------------------------------------------------------------------------
# Abandoned Carts — admin view (live CartItem data, inactive > 1 hour)
# ---------------------------------------------------------------------------

@router.get("/abandoned-carts")
async def admin_list_abandoned_carts(
    db: AsyncSession = Depends(get_db),
):
    from datetime import timedelta
    from app.models.order import CartItem
    from app.models.product import ProductVariant, Product
    from app.models.company import CompanyUser

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)

    result = await db.execute(
        select(CartItem)
        .where(CartItem.updated_at < cutoff)
        .order_by(CartItem.company_id, CartItem.updated_at.desc())
    )
    items = result.scalars().all()

    # Group by company_id
    company_map: dict[str, list] = {}
    for item in items:
        key = str(item.company_id)
        company_map.setdefault(key, []).append(item)

    out = []
    for company_id_str, cart_items in company_map.items():
        company = (await db.execute(
            select(Company).where(Company.id == cart_items[0].company_id)
        )).scalar_one_or_none()

        # Get owner email
        customer_email = None
        owner_row = (await db.execute(
            select(CompanyUser).where(
                CompanyUser.company_id == cart_items[0].company_id,
                CompanyUser.role == "owner",
            )
        )).scalar_one_or_none()
        if owner_row:
            owner_user = (await db.execute(
                select(User).where(User.id == owner_row.user_id)
            )).scalar_one_or_none()
            if owner_user:
                customer_email = owner_user.email

        items_detail = []
        total = 0.0
        for ci in cart_items:
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
            items_detail.append({
                "variant_id": str(ci.variant_id),
                "product_name": product_name,
                "sku": variant.sku if variant else "",
                "color": variant.color if variant else "",
                "size": variant.size if variant else "",
                "quantity": ci.quantity,
                "unit_price": unit,
                "line_total": line,
            })

        abandoned_at = max(ci.updated_at for ci in cart_items)
        out.append({
            "id": company_id_str,
            "company_name": company.name if company else "Unknown",
            "company_id": company_id_str,
            "customer_email": customer_email,
            "abandoned_at": abandoned_at.isoformat(),
            "total": round(total, 2),
            "item_count": len(cart_items),
            "items": items_detail,
            "is_recovered": False,
            "recovered_at": None,
        })

    return sorted(out, key=lambda x: x["abandoned_at"], reverse=True)


@router.post("/abandoned-carts/{company_id}/remind")
async def send_abandoned_cart_reminder(
    company_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    from datetime import timedelta
    from app.models.order import CartItem
    from app.models.product import ProductVariant, Product
    from app.models.company import CompanyUser
    from app.services.email_service import EmailService

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await db.execute(
        select(CartItem)
        .where(CartItem.company_id == company_id, CartItem.updated_at < cutoff)
    )
    cart_items = result.scalars().all()
    if not cart_items:
        raise HTTPException(status_code=404, detail="No abandoned cart items found")

    owner_row = (await db.execute(
        select(CompanyUser).where(
            CompanyUser.company_id == company_id, CompanyUser.role == "owner"
        )
    )).scalar_one_or_none()
    if not owner_row:
        raise HTTPException(status_code=404, detail="Company owner not found")
    owner = (await db.execute(select(User).where(User.id == owner_row.user_id))).scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="Owner user not found")

    rows_html = ""
    total = 0.0
    for ci in cart_items:
        variant = (await db.execute(
            select(ProductVariant).where(ProductVariant.id == ci.variant_id)
        )).scalar_one_or_none()
        prod = None
        if variant:
            prod = (await db.execute(
                select(Product).where(Product.id == variant.product_id)
            )).scalar_one_or_none()
        unit = float(ci.unit_price or 0)
        line = unit * ci.quantity
        total += line
        name = prod.name if prod else "Product"
        details = " / ".join(filter(None, [variant.color if variant else None, variant.size if variant else None]))
        details_html = f'<br><span style="font-size:11px;color:#9ca3af">{details}</span>' if details else ""
        rows_html += (
            f'<tr>'
            f'<td style="padding:8px 0;border-bottom:1px solid #f3f4f6">{name}{details_html}'
            f'</td>'
            f'<td style="padding:8px;border-bottom:1px solid #f3f4f6;text-align:center">{ci.quantity}</td>'
            f'<td style="padding:8px 0;border-bottom:1px solid #f3f4f6;text-align:right">${unit:.2f}</td>'
            f'</tr>'
        )

    EmailService(db).send_raw(
        to_email=owner.email,
        subject="You left items in your cart — AF Apparels",
        body_html=_af_email(
            f'<h2 style="color:#2A2830;margin:0 0 12px">Your cart is waiting!</h2>'
            f'<p>Hi {owner.first_name or "there"},</p>'
            f'<p>You have items saved in your AF Apparels cart. Complete your order before they sell out.</p>'
            f'<table style="width:100%;border-collapse:collapse;margin:16px 0">'
            f'<thead><tr>'
            f'<th style="text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;padding:0 0 8px">Product</th>'
            f'<th style="text-align:center;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;padding:0 8px 8px">Qty</th>'
            f'<th style="text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#6b7280;padding:0 0 8px">Price</th>'
            f'</tr></thead>'
            f'<tbody>{rows_html}</tbody>'
            f'<tfoot><tr>'
            f'<td colspan="2" style="padding:12px 0 0;text-align:right;font-weight:700;color:#2A2830">Total:</td>'
            f'<td style="padding:12px 0 0;text-align:right;font-weight:800;font-size:18px;color:#1A5CFF">${total:.2f}</td>'
            f'</tr></tfoot>'
            f'</table>'
            f'<p style="margin-top:24px">'
            f'<a href="https://shop.afapparels.com/cart" style="background:#1A5CFF;color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:700;display:inline-block">Complete Your Order</a>'
            f'</p>'
        ),
    )
    return {"message": f"Reminder sent to {owner.email}"}


@router.get("/orders/{order_id}/invoice-pdf")
async def download_admin_invoice_pdf(order_id: UUID, db: AsyncSession = Depends(get_db)):
    """Download any order's invoice as a PDF (admin). Uses the official QuickBooks
    invoice PDF when the order is synced to QB, otherwise generates a local one."""
    import io
    from sqlalchemy.orm import selectinload

    # Eager-load the relationships the invoice PDF reads (company + placed_by),
    # otherwise generating it lazy-loads them and crashes under async SQLAlchemy.
    order = (await db.execute(
        select(Order).options(
            selectinload(Order.items),
            selectinload(Order.company),
            selectinload(Order.placed_by),
        ).where(Order.id == order_id)
    )).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    filename = f"invoice-{order.order_number}.pdf"

    # Prefer the official QuickBooks invoice PDF when the order is synced there.
    if order.qb_invoice_id:
        try:
            import httpx as _httpx
            from app.services.quickbooks_service import QuickBooksService
            svc = await QuickBooksService().initialize()
            async with _httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    svc._url(f"invoice/{order.qb_invoice_id}/pdf"),
                    params={"minorversion": "65"},
                    headers={"Authorization": f"Bearer {svc._access_token}", "Accept": "application/pdf"},
                )
            if resp.status_code == 200:
                return StreamingResponse(
                    io.BytesIO(resp.content),
                    media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            logger.warning("QB PDF returned %s for invoice %s — using local PDF", resp.status_code, order.qb_invoice_id)
        except Exception as exc:
            logger.error("QB invoice PDF fetch failed: %s — using local PDF", exc)

    # Local PDF fallback (works even when the order isn't in QuickBooks)
    from app.services.pdf_service import PDFService
    try:
        pdf = PDFService().generate_invoice(order)
    except Exception as e:
        logger.error("Local invoice PDF generation failed for order %s: %s", order_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not generate the invoice PDF for this order.")
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Backorder queue ───────────────────────────────────────────────────────────

# Once an order leaves the building the backorder is settled, whatever the line
# still says. Anything before that is still owed.
_BACKORDER_OPEN_STATUSES = ("pending", "confirmed", "processing", "ready_for_pickup")


@router.get("/backorders", response_model=dict)
async def list_backorders(
    only_ready: bool = Query(False, description="Only lines whose stock has since arrived"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Orders taken while stock was short, and whether they can go out yet.

    Selling past zero is only safe if somebody can see what is owed. A line is
    listed from the moment it is sold short until the order ships — the flag on
    the line records how it was sold, and the variant's stock says whether the
    goods have since arrived.

    "Ready" means the shelf now holds enough for that line. Stock is a shared
    pool, so two backorders on the same variant can both look ready when only one
    can actually ship; oldest first is the order they should be filled in.
    """
    from sqlalchemy.orm import selectinload
    from app.models.inventory import InventoryRecord as _IR
    from app.models.product import ProductVariant as _PV
    from app.models.purchase_order import POLineItem as _POLI, PurchaseOrder as _PO

    rows = (await db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.is_backordered.is_(True))
        .where(Order.status.in_(_BACKORDER_OPEN_STATUSES))
        .options(selectinload(Order.company))
        .order_by(Order.created_at.asc())
    )).all()

    if not rows:
        return {"summary": {"lines": 0, "units": 0, "ready_lines": 0, "orders": 0}, "items": []}

    variant_ids = [oi.variant_id for oi, _ in rows if oi.variant_id]

    stock = {
        vid: int(q or 0)
        for vid, q in (await db.execute(
            select(_IR.variant_id, func.coalesce(func.sum(_IR.quantity), 0))
            .where(_IR.variant_id.in_(variant_ids))
            .group_by(_IR.variant_id)
        )).all()
    } if variant_ids else {}

    # When the next unreceived purchase order carrying this variant is due.
    due = {
        vid: d
        for vid, d in (await db.execute(
            select(_POLI.product_variant_id, func.min(_PO.expected_delivery))
            .join(_PO, _PO.id == _POLI.po_id)
            .where(_POLI.product_variant_id.in_(variant_ids))
            .where(_PO.expected_delivery.isnot(None))
            .where(_PO.status.notin_(["cancelled", "received"]))
            .group_by(_POLI.product_variant_id)
        )).all()
    } if variant_ids else {}

    still_allowed = {
        vid: bool(f)
        for vid, f in (await db.execute(
            select(_PV.id, _PV.allow_backorder).where(_PV.id.in_(variant_ids))
        )).all()
    } if variant_ids else {}

    items: list[dict] = []
    ready_lines = 0
    for oi, order in rows:
        on_hand = stock.get(oi.variant_id, 0)
        ready = on_hand >= int(oi.quantity or 0)
        if only_ready and not ready:
            continue
        if ready:
            ready_lines += 1
        items.append({
            "order_id": str(order.id),
            "order_number": order.order_number,
            "order_date": order.created_at.isoformat() if order.created_at else None,
            "order_status": order.status,
            "payment_status": order.payment_status,
            "company_name": order.company.name if order.company else (order.guest_name or "—"),
            "product_name": oi.product_name,
            "sku": oi.sku,
            "color": oi.color,
            "size": oi.size,
            "quantity": int(oi.quantity or 0),
            "stock_on_hand": on_hand,
            # Negative stock is the shortfall across every order waiting on this
            # variant, not just this line.
            "shortfall": max(0, -on_hand),
            "expected_restock_date": due.get(oi.variant_id).isoformat() if due.get(oi.variant_id) else None,
            "still_backorderable": still_allowed.get(oi.variant_id, False),
            "ready": ready,
        })

    return {
        "summary": {
            "lines": len(items),
            "units": sum(i["quantity"] for i in items),
            "ready_lines": ready_lines,
            "orders": len({i["order_number"] for i in items}),
        },
        "items": items,
    }
