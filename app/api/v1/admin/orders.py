"""Admin — order management and RMA."""
import csv
import io
import json as _json
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import NotFoundError
from app.models.company import Company
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.rma import RMAItem, RMARequest
from app.schemas.order import (
    AdminOrderDetail,
    AdminOrderListItem,
    CancelOrderRequest,
    OrderItemOut,
    OrderStatusUpdate,
    OrderUpdateRequest,
    RMACreate,
    RMAOut,
    RMAUpdateRequest,
)
from app.types.api import PaginatedResponse

router = APIRouter(prefix="/admin", tags=["admin-orders"])


def _af_email(content_html: str) -> str:
    """Wrap content in the AF Apparels branded email shell."""
    return (
        '<div style="font-family:sans-serif;max-width:600px;margin:0 auto">'
        '<div style="background:#080808;padding:24px;text-align:center">'
        '<span style="font-size:36px;font-weight:900;color:#1A5CFF">A</span>'
        '<span style="font-size:36px;font-weight:900;color:#E8242A">F</span>'
        '<span style="color:#fff;font-size:14px;margin-left:8px;letter-spacing:.1em">APPARELS</span>'
        '</div>'
        '<div style="padding:32px;background:#fff">'
        + content_html
        + '<p style="color:#9ca3af;font-size:12px;margin-top:24px">'
        'Questions? Call (214)&nbsp;272-7213 or email info.afapparel@gmail.com</p>'
        '<p style="color:#9ca3af;font-size:12px">— AF Apparels Team</p>'
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
            "processing": "In Production", "ready_for_pickup": "Ready for Pickup",
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
                tracking_block = ""
                if order.tracking_number:
                    carrier_line = (
                        f'<p style="margin:4px 0 0;color:#166534">Carrier: <b>{order.courier}</b></p>'
                        if order.courier else ""
                    )
                    tracking_block = (
                        '<div style="background:#f0fdf4;border:1px solid #bbf7d0;'
                        'border-radius:8px;padding:16px;margin:16px 0">'
                        '<p style="margin:0 0 4px;font-weight:700;color:#166534">Tracking Information</p>'
                        f'<p style="margin:0;color:#166534">Tracking #: <b>{order.tracking_number}</b></p>'
                        f'{carrier_line}</div>'
                    )
                email_svc.send_raw(
                    to_email=order.guest_email,
                    subject=f"Your Order {order.order_number} Has Shipped!",
                    body_html=_af_email(
                        f'<h2 style="color:#059669;margin:0 0 12px">Your Order Has Shipped! &#128230;</h2>'
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
                email_svc.send_raw(
                    to_email=user.email,
                    subject=f"Order {order.order_number} Has Shipped!",
                    body_html=_af_email(
                        f'<h2 style="color:#059669;margin:0 0 12px">Your Order Has Shipped! &#128230;</h2>'
                        f'<p>Hi {first},</p>'
                        f'<p>Order <b>{order.order_number}</b> is on its way.</p>'
                        + (f'<p><b>Tracking #:</b> {order.tracking_number}</p>' if order.tracking_number else "")
                        + (f'<p><b>Carrier:</b> {order.courier}</p>' if order.courier else "")
                        + f'<p style="margin:20px 0"><a href="{order_url}"'
                        f' style="background:#E8242A;color:#fff;padding:12px 24px;border-radius:6px;'
                        f'text-decoration:none;font-weight:700;display:inline-block">View Order &rarr;</a></p>'
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
    payload: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create an empty draft (pending) order for admin to fill in."""
    from uuid import UUID as _UUID
    from app.models.company import Company as _Company, CompanyUser as _CompanyUser
    import string, random

    company_id_str = payload.get("company_id")
    if not company_id_str:
        raise HTTPException(status_code=422, detail="company_id is required")

    company_id = _UUID(str(company_id_str))

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

    # Generate order number
    suffix = "".join(random.choices(string.digits, k=6))
    order_number = f"DRAFT-{suffix}"

    order = Order(
        company_id=company_id,
        placed_by_id=member.user_id,
        order_number=order_number,
        status="pending",
        payment_status="unpaid",
        po_number=payload.get("po_number"),
        notes=payload.get("notes"),
        subtotal=0,
        shipping_cost=0,
        tax_amount=0,
        total=0,
    )
    db.add(order)
    await db.commit()
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

    items = []
    for row in rows:
        order, company_name = row
        item_count = (await db.execute(
            select(func.count(OrderItem.id)).where(OrderItem.order_id == order.id)
        )).scalar_one()
        items.append(AdminOrderListItem(
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
            guest_email=order.guest_email,
            guest_name=order.guest_name,
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
async def get_admin_order(order_id: UUID, db: AsyncSession = Depends(get_db)):
    from sqlalchemy import outerjoin
    result = await db.execute(
        select(Order, Company.name.label("company_name"))
        .select_from(outerjoin(Order, Company, Order.company_id == Company.id))
        .where(Order.id == order_id)
    )
    row = result.one_or_none()
    if not row:
        raise NotFoundError(f"Order {order_id} not found")
    order, company_name = row

    items_result = await db.execute(select(OrderItem).where(OrderItem.order_id == order_id))
    items = items_result.scalars().all()

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
            # Normalize to frontend-expected keys
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
        total=order.total,
        company_id=order.company_id,
        company_name=company_name,
        tracking_number=order.tracking_number,
        courier=order.courier,
        courier_service=order.courier_service,
        shipped_at=order.shipped_at,
        qb_invoice_id=order.qb_invoice_id,
        created_at=order.created_at,
        updated_at=order.updated_at,
        items=[OrderItemOut.model_validate(i) for i in items],
        customer_name=customer_name,
        customer_email=customer_email,
        customer_phone=customer_phone,
        shipping_address=shipping_address,
        is_guest_order=order.is_guest_order,
        guest_email=order.guest_email,
        guest_name=order.guest_name,
        guest_phone=order.guest_phone,
        payment_method=getattr(order, "payment_method", None),
        ach_bank_name=getattr(order, "ach_bank_name", None),
        ach_account_holder=getattr(order, "ach_account_holder", None),
        ach_account_last4=getattr(order, "ach_account_last4", None),
        ach_account_type=getattr(order, "ach_account_type", None),
        ach_verified=getattr(order, "ach_verified", None),
    )


@router.post("/orders/{order_id}/verify-ach", status_code=200)
async def verify_ach_payment(order_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Mark an ACH order as payment verified (admin confirms bank transfer received)."""
    from sqlalchemy import text as _text
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    try:
        await db.execute(
            _text("UPDATE orders SET ach_verified=true, payment_status='paid' WHERE id=:oid"),
            {"oid": str(order_id)},
        )
        await db.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "verified", "order_id": str(order_id)}


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
    if order.status not in ("pending", "confirmed"):
        raise HTTPException(status_code=422, detail="Can only add items to pending orders")

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

    # Use provided unit_price or fall back to variant retail price
    unit_price = float(payload.get("unit_price") or variant.retail_price or 0)
    line_total = unit_price * quantity

    # Fetch product info for denormalized fields
    from app.models.product import Product
    product = (await db.execute(
        select(Product).where(Product.id == variant.product_id)
    )).scalar_one_or_none()

    item = OrderItem(
        order_id=order_id,
        variant_id=variant_id,
        quantity=quantity,
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
    order.total = float(order.subtotal) + float(order.shipping_cost or 0) + float(order.tax_amount or 0)

    await db.commit()
    return {"message": "Item added", "item_id": str(item.id), "subtotal": float(order.subtotal), "total": float(order.total)}


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
    if order and order.status in ("pending", "confirmed"):
        order.subtotal = max(0, float(order.subtotal or 0) - float(item.line_total or 0))
        order.total = float(order.subtotal) + float(order.shipping_cost or 0) + float(order.tax_amount or 0)

    await db.delete(item)
    await db.commit()
    return {"message": "Item removed"}


@router.patch("/orders/{order_id}", response_model=dict)
async def update_admin_order(
    order_id: UUID,
    payload: OrderUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    old_status = order.status
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(order, field, value)
    await db.commit()

    if payload.status and payload.status != old_status:
        await _send_order_status_email(order, payload.status, db)

    return {"message": "Order updated"}


@router.patch("/orders/{order_id}/status", response_model=dict)
async def update_order_status(
    order_id: UUID,
    payload: OrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    old_status = order.status
    order.status = payload.status

    if payload.tracking_number is not None:
        order.tracking_number = payload.tracking_number
    if payload.courier is not None:
        order.courier = payload.courier
    if payload.courier_service is not None:
        order.courier_service = payload.courier_service
    if payload.status == "shipped" and not order.shipped_at:
        order.shipped_at = datetime.now(timezone.utc)

    await db.commit()

    if payload.status != old_status:
        await _send_order_status_email(order, payload.status, db)

    return {"success": True, "status": order.status}


@router.post("/orders/{order_id}/cancel", response_model=dict)
async def cancel_admin_order(
    order_id: UUID,
    payload: CancelOrderRequest,
    db: AsyncSession = Depends(get_db),
):
    order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    order.status = "cancelled"
    if hasattr(order, "notes"):
        order.notes = f"Cancelled: {payload.reason}"
    await db.commit()

    await _send_order_status_email(order, "cancelled", db)

    return {"message": "Order cancelled"}


@router.post("/orders/{order_id}/sync-quickbooks", response_model=dict)
async def sync_order_to_quickbooks(order_id: UUID, db: AsyncSession = Depends(get_db)):
    from app.tasks.quickbooks_tasks import sync_order_to_qb
    sync_order_to_qb.delay(str(order_id))
    return {"message": "QuickBooks sync queued", "order_id": str(order_id)}


# ---------------------------------------------------------------------------
# Admin RMA management
# ---------------------------------------------------------------------------

@router.get("/rma")
async def list_admin_rma(
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    query = select(RMARequest)
    if status:
        query = query.where(RMARequest.status == status)
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(
        query.order_by(RMARequest.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return PaginatedResponse(
        items=list(result.scalars().all()),
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


@router.patch("/rma/{rma_id}", response_model=dict)
async def update_rma(
    rma_id: UUID,
    payload: RMAUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    rma = (await db.execute(select(RMARequest).where(RMARequest.id == rma_id))).scalar_one_or_none()
    if not rma:
        raise NotFoundError(f"RMA {rma_id} not found")
    rma.status = payload.status
    if payload.admin_notes:
        rma.admin_notes = payload.admin_notes
    await db.commit()

    # Notify customer of status change
    try:
        from app.tasks.email_tasks import send_rma_status_email
        send_rma_status_email.delay(str(rma_id))
    except Exception:
        pass

    return {"message": f"RMA {payload.status}"}


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