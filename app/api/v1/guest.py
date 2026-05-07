"""Guest checkout endpoints — no authentication required."""
import json
import logging
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import NotFoundError, ValidationError, InsufficientStockError
from app.models.inventory import InventoryRecord
from app.models.order import Order, OrderItem
from app.models.product import Product, ProductVariant
from app.schemas.order import AddressIn

router = APIRouter(prefix="/guest", tags=["guest"])

logger = logging.getLogger(__name__)

GUEST_SHIPPING_STANDARD = Decimal("9.99")
GUEST_SHIPPING_EXPEDITED = Decimal("54.99")  # standard + expedited surcharge


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class GuestCartItem(BaseModel):
    variant_id: UUID
    quantity: int


class GuestCheckoutRequest(BaseModel):
    guest_name: str
    guest_email: str
    guest_phone: str | None = None
    items: list[GuestCartItem]
    shipping_address: AddressIn
    shipping_method: str = "standard"  # standard | expedited | will_call
    payment_method: str = "card"  # card | ach
    qb_token: str | None = None
    ach_bank_name: str | None = None
    ach_account_holder: str | None = None
    ach_routing_number: str | None = None
    ach_account_last4: str | None = None
    ach_account_type: str | None = None
    order_notes: str | None = None
    tax_amount: Decimal | None = None
    tax_rate: float | None = None
    tax_region: str | None = None


class GuestOrderOut(BaseModel):
    order_id: str
    order_number: str
    total: float
    status: str


# ---------------------------------------------------------------------------
# POST /api/v1/guest/checkout
# ---------------------------------------------------------------------------

@router.post("/checkout", status_code=201)
async def guest_checkout(
    payload: GuestCheckoutRequest,
    db: AsyncSession = Depends(get_db),
) -> GuestOrderOut:
    """Place an order as a guest (retail pricing, no account required)."""
    from app.core.config import get_settings

    settings = get_settings()

    if not payload.items:
        raise ValidationError("Cart is empty")

    # 1. Validate + price each item using MSRP
    order_items_data = []
    subtotal = Decimal("0")

    for cart_item in payload.items:
        if cart_item.quantity < 1:
            raise ValidationError("Quantity must be at least 1")

        variant_result = await db.execute(
            select(ProductVariant, Product)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(ProductVariant.id == cart_item.variant_id)
        )
        row = variant_result.first()
        if not row:
            raise NotFoundError(f"Variant {cart_item.variant_id} not found")
        variant, product = row

        if variant.status != "active":
            raise ValidationError(f"SKU {variant.sku} is no longer available")

        # Stock check — 0 means unlimited
        stock_result = await db.execute(
            select(func.coalesce(func.sum(InventoryRecord.quantity), 0))
            .where(InventoryRecord.variant_id == variant.id)
        )
        available = stock_result.scalar_one()
        if available > 0 and available < cart_item.quantity:
            raise InsufficientStockError(
                f"Only {available} units available for {variant.sku}"
            )

        # Guest price = MSRP if set, else retail_price
        unit_price = Decimal(str(variant.msrp or variant.retail_price or 0))
        line_total = unit_price * cart_item.quantity
        subtotal += line_total

        order_items_data.append({
            "variant_id": variant.id,
            "product_name": product.name,
            "sku": variant.sku,
            "color": variant.color,
            "size": variant.size,
            "quantity": cart_item.quantity,
            "unit_price": unit_price,
            "line_total": line_total,
        })

    # 2. Shipping cost
    method = payload.shipping_method or "standard"
    if method == "will_call":
        shipping_cost = Decimal("0")
    elif method == "expedited":
        shipping_cost = GUEST_SHIPPING_EXPEDITED
    else:
        shipping_cost = GUEST_SHIPPING_STANDARD

    tax_amount_val = payload.tax_amount or Decimal("0")
    total = subtotal + shipping_cost + tax_amount_val

    # 3. Charge card via QB Payments (skip for ACH — verified manually)
    if payload.payment_method == "ach":
        qb_charge_id = None
        qb_payment_status = "ACH_PENDING"
        _payment_status = "pending"
    else:
        if not payload.qb_token:
            raise ValidationError("Card token is required for card payments")
        from app.services.qb_payments_service import QBPaymentsService
        qb_pay = QBPaymentsService()
        try:
            charge_resp = qb_pay.charge_card(
                token=payload.qb_token,
                amount=float(total),
                description=f"AF Apparels guest order — {payload.guest_email}",
            )
        except RuntimeError as exc:
            raise ValidationError(f"Payment failed: {exc}") from exc

        qb_charge_id = charge_resp.get("id")
        qb_payment_status = charge_resp.get("status", "UNKNOWN")
        _payment_status = "paid" if qb_payment_status == "CAPTURED" else "pending"

    # 4. Generate order number — query DB for max to avoid Redis-reset collisions
    from sqlalchemy import text as _text
    try:
        _num_result = await db.execute(_text(
            "SELECT order_number FROM orders "
            "WHERE order_number LIKE 'AF-%' "
            "ORDER BY SUBSTRING(order_number FROM 4)::INTEGER DESC "
            "LIMIT 1"
        ))
        _num_row = _num_result.fetchone()
        if _num_row and _num_row[0]:
            _counter = int(_num_row[0].split("-", 1)[-1]) + 1
        else:
            _counter = 1
    except Exception:
        import random
        _counter = random.randint(10000, 99999)
    order_number = f"AF-{_counter:06d}"

    # 5. Create Order record
    address_snapshot = json.dumps({
        "full_name": payload.guest_name,
        "line1": payload.shipping_address.line1,
        "line2": payload.shipping_address.line2,
        "city": payload.shipping_address.city,
        "state": payload.shipping_address.state,
        "postal_code": payload.shipping_address.postal_code,
        "country": payload.shipping_address.country,
        "phone": payload.guest_phone,
    })

    order = Order(
        order_number=order_number,
        company_id=None,
        placed_by_id=None,
        is_guest_order=True,
        guest_email=payload.guest_email.lower().strip(),
        guest_name=payload.guest_name,
        guest_phone=payload.guest_phone,
        status="pending",
        payment_status=_payment_status,
        notes=payload.order_notes,
        qb_payment_charge_id=qb_charge_id,
        qb_payment_status=qb_payment_status,
        payment_method=payload.payment_method,
        ach_bank_name=payload.ach_bank_name if payload.payment_method == "ach" else None,
        ach_account_holder=payload.ach_account_holder if payload.payment_method == "ach" else None,
        ach_routing_number=payload.ach_routing_number if payload.payment_method == "ach" else None,
        ach_account_last4=payload.ach_account_last4 if payload.payment_method == "ach" else None,
        ach_account_type=payload.ach_account_type if payload.payment_method == "ach" else None,
        subtotal=subtotal,
        shipping_cost=shipping_cost,
        tax_amount=tax_amount_val,
        tax_rate=payload.tax_rate,
        tax_region=payload.tax_region,
        total=total,
        shipping_method=method,
        shipping_address_snapshot=address_snapshot,
    )
    db.add(order)
    await db.flush()

    # 6. Create OrderItem records + deduct inventory
    from sqlalchemy import update as _update

    for item_data in order_items_data:
        db.add(OrderItem(order_id=order.id, **item_data))

        qty_to_deduct = int(item_data["quantity"])
        inv_result = await db.execute(
            select(InventoryRecord)
            .where(InventoryRecord.variant_id == item_data["variant_id"])
            .order_by(InventoryRecord.quantity.desc())
        )
        for record in inv_result.scalars().all():
            if qty_to_deduct <= 0:
                break
            deduct = min(int(record.quantity), qty_to_deduct)
            if deduct > 0:
                await db.execute(
                    _update(InventoryRecord)
                    .where(InventoryRecord.id == record.id)
                    .values(quantity=int(record.quantity) - deduct)
                )
                qty_to_deduct -= deduct

    await db.flush()

    # 7. Reload with items eager-loaded
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    order = result.scalar_one()

    # 8. Send guest confirmation email + admin alert
    try:
        from app.services.email_service import EmailService
        _email_svc = EmailService(db)
        _email_svc.send_order_confirmation(order, order.guest_email)
        _email_svc.send_admin_new_order_alert(order)
    except Exception as exc:
        logger.warning("Order confirmation email failed: %s", exc)

    await db.commit()

    return GuestOrderOut(
        order_id=str(order.id),
        order_number=order.order_number,
        total=float(order.total),
        status=order.status,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/guest/shipping-estimate
# ---------------------------------------------------------------------------

@router.get("/shipping-estimate")
async def guest_shipping_estimate(
    units: int = Query(0, ge=0),
    subtotal: float = Query(0.0, ge=0.0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return standard shipping cost for a guest cart (uses platform standard_shipping setting)."""
    from app.models.system import Settings

    try:
        std_row = (await db.execute(
            select(Settings).where(Settings.key == "standard_shipping")
        )).scalar_one_or_none()

        if std_row and std_row.value:
            cfg = json.loads(std_row.value)
            shipping_type = cfg.get("shipping_type", "store_default")

            if shipping_type == "store_default":
                return {"estimated_shipping": float(cfg.get("shipping_amount", 9.99))}

            if shipping_type == "flat_rate" and cfg.get("brackets"):
                calc_type = cfg.get("calc_type", "order_value")
                value = units if calc_type == "units" else subtotal
                for bracket in cfg["brackets"]:
                    min_k = "min_units" if calc_type == "units" else "min_order_value"
                    max_k = "max_units" if calc_type == "units" else "max_order_value"
                    min_val = bracket.get(min_k) or 0
                    max_val = bracket.get(max_k)
                    if value >= min_val and (max_val is None or value <= max_val):
                        return {"estimated_shipping": float(bracket.get("cost", 9.99))}
    except Exception:
        pass

    return {"estimated_shipping": float(GUEST_SHIPPING_STANDARD)}


# ---------------------------------------------------------------------------
# GET /api/v1/guest/orders/{order_number}?email={email}
# ---------------------------------------------------------------------------

@router.get("/orders/{order_number}")
async def track_guest_order(
    order_number: str,
    email: str = Query(..., description="Guest email used at checkout"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Look up a guest order by order number + email."""
    from sqlalchemy.orm import selectinload

    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(
            Order.order_number == order_number,
            Order.is_guest_order == True,
            Order.guest_email == email.lower().strip(),
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError("Order not found. Please check your order number and email.")

    return {
        "order_number": order.order_number,
        "status": order.status,
        "payment_status": order.payment_status,
        "subtotal": float(order.subtotal),
        "shipping_cost": float(order.shipping_cost),
        "total": float(order.total),
        "created_at": order.created_at.isoformat(),
        "guest_name": order.guest_name,
        "items": [
            {
                "product_name": i.product_name,
                "sku": i.sku,
                "color": i.color,
                "size": i.size,
                "quantity": i.quantity,
                "unit_price": float(i.unit_price),
                "line_total": float(i.line_total),
            }
            for i in order.items
        ],
    }
