"""Guest checkout endpoints — no authentication required."""
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
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
from app.models.user import User
from app.models.product import Product, ProductVariant
from app.schemas.order import AddressIn

router = APIRouter(prefix="/guest", tags=["guest"])

logger = logging.getLogger(__name__)


async def _create_or_get_retail_user(
    email: str,
    first_name: str,
    last_name: str,
    db: AsyncSession,
) -> tuple:
    """Create (or fetch) a retail User account for a guest shopper.

    Returns (user, activation_token_or_None).
    activation_token is None when the user already exists.
    """
    from app.models.user import User

    result = await db.execute(select(User).where(User.email == email.lower()))
    existing = result.scalar_one_or_none()
    if existing:
        return existing, None

    token = secrets.token_urlsafe(32)
    token_expires = datetime.now(timezone.utc) + timedelta(days=7)

    new_user = User(
        email=email.lower(),
        first_name=first_name,
        last_name=last_name,
        account_type="retail",
        is_active=False,
        hashed_password=None,
        activation_token=token,
        activation_token_expires=token_expires,
    )
    db.add(new_user)
    await db.flush()
    return new_user, token

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
    shipping_cost: Decimal | None = None
    shipping_rate_id: str | None = None
    shipping_carrier: str | None = None
    shipping_service: str | None = None


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
    ordered_product_slugs: set[str] = set()
    subtotal = Decimal("0")

    for cart_item in payload.items:
        if cart_item.quantity < 1:
            raise ValidationError("Quantity must be at least 1")

        variant_result = await db.execute(
            select(ProductVariant, Product)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(ProductVariant.id == cart_item.variant_id)
            .with_for_update(skip_locked=False)
        )
        row = variant_result.first()
        if not row:
            raise NotFoundError(f"Variant {cart_item.variant_id} not found")
        variant, product = row
        if product.slug:
            ordered_product_slugs.add(product.slug)

        if variant.status != "active":
            import logging as _log
            _log.getLogger(__name__).warning(
                "Checkout blocked: variant %s (SKU %s) has status '%s'",
                variant.id, variant.sku, variant.status
            )
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

    # 2. Shipping cost — client value is authoritative when provided
    method = payload.shipping_method or "standard"
    if method == "will_call":
        shipping_cost = Decimal("0")
    elif method == "expedited":
        shipping_cost = GUEST_SHIPPING_EXPEDITED
    else:
        shipping_cost = GUEST_SHIPPING_STANDARD
    if payload.shipping_cost and payload.shipping_cost > 0:
        shipping_cost = payload.shipping_cost

    tax_amount_val = payload.tax_amount or Decimal("0")
    convenience_fee = (subtotal * Decimal("0.03")).quantize(Decimal("0.01")) if payload.payment_method == "card" else Decimal("0.00")
    total = subtotal + shipping_cost + tax_amount_val + convenience_fee

    # 3. Charge card via QB Payments (skip for ACH — collected manually)
    if payload.payment_method == "ach":
        qb_charge_id = None
        qb_payment_status = "ACH_PENDING"
        _payment_status = "paid"  # ACH / bank transfer treated as immediately paid
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
        # Card charge succeeded (RuntimeError raised on any failure above),
        # so the order is paid regardless of the specific status string QB returns.
        # This mirrors order_service.create_order which uses payment_method, not
        # qb_payment_status, to determine payment_status.
        _payment_status = "paid"

    # 4. Generate order number — delegate to the single shared generator so
    #    retail/guest and wholesale order numbers form one sequential series.
    from app.services.order_service import OrderService as _OrderSvc
    order_number = await _OrderSvc(db)._generate_order_number()

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

    # Save shipping_rate_id + carrier from customer's selected live Shippo rate
    logger.info(
        "Guest order create - shipping_rate_id: %s, carrier: %s",
        payload.shipping_rate_id, payload.shipping_carrier,
    )
    if payload.shipping_rate_id:
        try:
            await db.execute(
                _text(
                    "UPDATE orders SET shipping_rate_id=:rid, carrier=:car, courier_service=:cs WHERE id=:oid"
                ),
                {"rid": payload.shipping_rate_id, "car": payload.shipping_carrier,
                 "cs": payload.shipping_service, "oid": str(order.id)},
            )
        except Exception as _exc:
            logger.warning("Could not save shipping_rate_id on guest order %s: %s", order.id, _exc)

    if convenience_fee > 0:
        try:
            await db.execute(
                _text("UPDATE orders SET convenience_fee=:cf WHERE id=:oid"),
                {"cf": float(convenience_fee), "oid": str(order.id)},
            )
        except Exception as _exc:
            logger.warning("Could not save convenience_fee on guest order %s: %s", order.id, _exc)

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

        # Sync updated stock to QB after each variant deduction
        try:
            from app.tasks.quickbooks_tasks import sync_inventory_to_qb as _siqb
            _siqb.apply_async(args=[str(item_data["variant_id"])], countdown=15)
        except Exception as _exc:
            logger.warning("QB inventory sync dispatch failed: %s", _exc)

    # Bust product detail Redis cache so stock shows correctly for everyone
    try:
        from app.core.redis import redis_delete_pattern as _rdp
        for _slug in ordered_product_slugs:
            await _rdp(f"products:detail:{_slug}:*")
        if ordered_product_slugs:
            await _rdp("products:list:*")
    except Exception:
        pass

    await db.flush()

    # 7. Auto-create retail account for this guest (non-blocking)
    _activation_token: str | None = None
    try:
        _retail_user, _activation_token = await _create_or_get_retail_user(
            email=payload.guest_email,
            first_name=payload.guest_name.split()[0] if payload.guest_name else "Guest",
            last_name=" ".join(payload.guest_name.split()[1:]) if payload.guest_name and len(payload.guest_name.split()) > 1 else "",
            db=db,
        )
        order.placed_by_id = _retail_user.id
        await db.flush()
    except Exception as exc:
        logger.warning("Retail user creation failed for %s: %s", payload.guest_email, exc)

    # 8. Reload with items eager-loaded
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    order = result.scalar_one()

    # 9. Send guest confirmation email + admin alert + activation email
    try:
        from app.services.email_service import EmailService
        from app.core.config import get_settings as _get_settings
        _email_svc = EmailService(db)
        _email_svc.send_order_confirmation(order, order.guest_email)
        _email_svc.send_admin_new_order_alert(order)
        if _activation_token:
            _cfg = _get_settings()
            _email_svc.send_retail_account_activation(
                customer_email=order.guest_email,
                first_name=order.guest_name.split()[0] if order.guest_name else "Guest",
                activation_url=f"{_cfg.FRONTEND_URL}/activate-account?token={_activation_token}",
                order_number=order.order_number,
            )
    except Exception as exc:
        logger.warning("Order confirmation email failed: %s", exc)

    await db.commit()

    # ── QB invoice sync ───────────────────────────────────────────────────────
    try:
        from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
        sync_order_invoice_to_qb.apply_async(args=[str(order.id)], countdown=5)
        logger.info("QB invoice sync queued for guest order %s", order.order_number)
    except Exception as _exc:
        logger.warning("QB invoice sync dispatch failed for %s: %s", order.order_number, _exc)

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
    email: str = Query(..., description="Email address used at checkout or on account"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Look up an order by order number + email (guest or registered user)."""
    from sqlalchemy import or_
    from sqlalchemy.orm import selectinload, outerjoin

    email_lower = email.lower().strip()
    order_number_clean = order_number.strip()

    logger.info("Track order lookup: order_number=%r email=%r", order_number_clean, email_lower)

    # Match guest orders on guest_email OR registered-user orders via the placed_by User record
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .outerjoin(User, User.id == Order.placed_by_id)
        .where(
            Order.order_number == order_number_clean,
            or_(
                func.lower(Order.guest_email) == email_lower,
                func.lower(User.email) == email_lower,
            ),
        )
    )
    order = result.scalar_one_or_none()

    logger.info(
        "Track order result: found=%s is_guest=%s guest_email=%r",
        order is not None,
        getattr(order, "is_guest_order", None),
        getattr(order, "guest_email", None),
    )

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
        "tracking_number": order.tracking_number,
        "tracking_url": getattr(order, "tracking_url", None),
        "carrier": order.courier,
        "courier_service": order.courier_service,
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
