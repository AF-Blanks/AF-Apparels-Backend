"""Guest checkout endpoints — no authentication required."""
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import (
    NotFoundError, PaymentError, ValidationError, InsufficientStockError, WholesaleAccountExistsError,
)
from app.models.inventory import InventoryRecord
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.product import Product, ProductVariant
from app.schemas.order import AddressIn

router = APIRouter(prefix="/guest", tags=["guest"])

logger = logging.getLogger(__name__)


def _dispatch_qb_inventory_sync(variant_ids: list[str], *, countdown: int, context: str) -> None:
    """Queue a SINGLE batched QB inventory-sync task for many variants.

    This is the optimization: instead of one Celery task (and its 2 QB calls +
    retry amplification) per variant, we queue ONE task for the whole batch.

    Safety: if the batch task is not present in quickbooks_tasks.py yet, we fall
    back to the original per-variant dispatch so inventory sync is NEVER silently
    dropped. No existing behaviour is lost.
    """
    if not variant_ids:
        return
    try:
        from app.tasks.quickbooks_tasks import sync_inventory_batch_to_qb as _batch
        _batch.apply_async(args=[variant_ids], countdown=countdown)
        logger.info(
            "QB inventory batch sync queued for %d variants (%s)",
            len(variant_ids), context,
        )
        return
    except (ImportError, AttributeError):
        logger.warning(
            "sync_inventory_batch_to_qb not available; falling back to per-variant dispatch (%s)",
            context,
        )
    except Exception as exc:
        logger.warning("QB inventory batch sync dispatch failed (%s): %s", context, exc)
        return

    # Fallback — preserve prior behaviour rather than skipping the sync entirely.
    try:
        from app.tasks.quickbooks_tasks import sync_inventory_to_qb as _single
        for _vid in variant_ids:
            _single.apply_async(args=[_vid], countdown=countdown)
    except Exception as exc:
        logger.warning("QB inventory per-variant fallback dispatch failed (%s): %s", context, exc)


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
    # Passed straight to QuickBooks to raise the debit and never written down;
    # only the last four digits are kept.
    ach_account_number: str | None = None
    ach_first_name: str | None = None
    ach_last_name: str | None = None
    ach_phone: str | None = None
    ach_account_ownership: str | None = None   # "personal" | "business"
    ach_authorized: bool = False
    ach_authorization_text: str | None = None
    order_notes: str | None = None
    discount_code: str | None = None
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
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GuestOrderOut:
    """Place an order as a guest (retail pricing, no account required)."""
    from app.core.config import get_settings

    settings = get_settings()

    if not payload.items:
        raise ValidationError("Cart is empty")

    # 0. Block guest checkout when this email already belongs to a wholesale
    #    account. Without this, the auto-link in step 7 below would silently
    #    attach the order to that account's placed_by_id with zero
    #    authentication (no password, no session) — at retail pricing, not
    #    billed to their company. They must log in instead.
    existing_account_result = await db.execute(
        select(User).where(User.email == payload.guest_email.lower().strip())
    )
    existing_account = existing_account_result.scalar_one_or_none()
    if existing_account and existing_account.account_type == "wholesale":
        raise WholesaleAccountExistsError()

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

        # Stock check — 0 with no records at all means untracked, not sold out
        stock_result = await db.execute(
            select(
                func.coalesce(func.sum(InventoryRecord.quantity), 0),
                func.count(InventoryRecord.id),
            ).where(InventoryRecord.variant_id == variant.id)
        )
        available, _rec_count = stock_result.one()
        # Guests buy the same goods off the same shelf, so a variant marked for
        # backorder is sellable here too. Refusing them while accepting the same
        # line at wholesale checkout would be an accident of which door they came
        # through, not a decision anyone made.
        _backordered = (
            bool(getattr(variant, "allow_backorder", False))
            and int(_rec_count or 0) > 0
            and available < cart_item.quantity
        )
        if not _backordered and available > 0 and available < cart_item.quantity:
            raise InsufficientStockError(
                f"Only {available} units available for {variant.sku}"
            )

        # Guest price = MSRP if set, else retail_price
        unit_price = Decimal(str(variant.msrp or variant.retail_price or 0))
        line_total = unit_price * cart_item.quantity
        subtotal += line_total

        order_items_data.append({
            "variant_id": variant.id,
            "is_backordered": _backordered,
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
    # Guard against a tampered/near-zero client-supplied shipping cost (e.g.
    # "shipping_cost: 0.01") slipping through the ">0" check above.
    if method != "will_call" and shipping_cost < Decimal("1.00"):
        raise ValidationError("Invalid shipping cost")

    tax_amount_val = payload.tax_amount or Decimal("0")
    if tax_amount_val < 0:
        raise ValidationError("Invalid tax amount")
    convenience_fee = Decimal("0.00")  # Guest/retail orders never incur a convenience fee

    # Discount code — validated server-side (never trust a client-supplied
    # amount). A guest can only use "all customers" codes; wholesale-only codes
    # are rejected. The discount reduces the amount actually charged.
    coupon_discount = Decimal("0")
    if payload.discount_code:
        from app.api.v1.discounts import validate_discount_code, compute_discount_amount
        _dc, _dc_err = await validate_discount_code(
            payload.discount_code, float(subtotal), None, "guest", db
        )
        if _dc_err:
            raise ValidationError(f"Discount code: {_dc_err}")
        coupon_discount = Decimal(str(compute_discount_amount(_dc, float(subtotal))))

    net_subtotal = max(Decimal("0"), subtotal - coupon_discount)
    total = net_subtotal + shipping_cost + tax_amount_val + convenience_fee

    # 3. Take the money. A card is charged here and either works or does not; a
    #    bank debit is raised after the order exists, because it clears over days
    #    and there is nothing to wait for at this point.
    if payload.payment_method == "ach":
        from app.services.qb_payments_service import QBPaymentsService as _QBPaySvc

        # Checked before the order is written, so a mistyped routing number is
        # something the customer can still fix.
        if not payload.ach_authorized:
            raise ValidationError("Please authorise the bank transfer before placing the order.")
        if len("".join(c for c in (payload.ach_account_number or "") if c.isdigit())) < 4:
            raise ValidationError("Please enter your full bank account number.")
        if not _QBPaySvc.routing_number_is_valid(payload.ach_routing_number):
            raise ValidationError("That routing number doesn't look right — please check the nine digits.")
        if not (payload.ach_first_name or "").strip() or not (payload.ach_last_name or "").strip():
            raise ValidationError("Please enter the first and last name on the bank account.")

        qb_charge_id = None
        qb_payment_status = "ACH_PENDING"
        # Not paid: the money is still in the customer's bank. It settles when
        # the debit clears, which settle_pending_echecks watches for.
        _payment_status = "unpaid"
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
        # The charge call not raising an exception only means QuickBooks
        # accepted the request — a declined card returns normally with
        # status="DECLINED", no exception. Without this check the order
        # still went through as "paid" with nothing actually collected.
        # charge_card captures by default, so success returns "CAPTURED";
        # any other status aborts here before the order is created.
        if qb_payment_status != "CAPTURED":
            raise PaymentError(
                f"Payment was not approved (status: {qb_payment_status}). "
                "Please check your card details or try a different payment method."
            )
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
        shipping_rate_id=payload.shipping_rate_id,
        carrier=payload.shipping_carrier,
        courier_service=payload.shipping_service,
    )
    db.add(order)
    await db.flush()
    logger.info(
        "Guest order create - shipping_rate_id: %s, carrier: %s",
        order.shipping_rate_id, order.carrier,
    )

    if convenience_fee > 0:
        try:
            await db.execute(
                _text("UPDATE orders SET convenience_fee=:cf WHERE id=:oid"),
                {"cf": float(convenience_fee), "oid": str(order.id)},
            )
        except Exception as _exc:
            logger.warning("Could not save convenience_fee on guest order %s: %s", order.id, _exc)

    # ── Bank debit ────────────────────────────────────────────────────────────
    # Raised against the order's own total so what leaves the customer's bank is
    # exactly what the invoice says. A failure here does not undo the order —
    # the customer has placed it and nothing about it is wrong — it is recorded
    # and the office is told, because collecting is then somebody's job here.
    if payload.payment_method == "ach":
        from app.services.ach_authorization import record_authorization as _record_auth
        from app.services.qb_payments_service import QBPaymentsService as _QBPaySvc

        # Filed before the debit is raised — see the wholesale checkout.
        await _record_auth(db, order.id, request, payload.ach_authorization_text)

        try:
            _echeck = _QBPaySvc().charge_echeck(
                amount=float(order.total),
                routing_number=payload.ach_routing_number or "",
                account_number=payload.ach_account_number or "",
                account_type=_QBPaySvc.echeck_account_type(
                    payload.ach_account_ownership, payload.ach_account_type
                ),
                first_name=payload.ach_first_name or "",
                last_name=payload.ach_last_name or "",
                phone=payload.ach_phone,
                description=f"AF Apparels order {order.order_number}",
            )
            _echeck_id = str(_echeck.get("id") or "")
            _echeck_status = str(_echeck.get("status") or "PENDING").upper()
            logger.info(
                "Guest eCheck raised for order %s — id=%s status=%s",
                order.order_number, _echeck_id, _echeck_status,
            )
        except Exception as _ach_exc:
            _echeck_id, _echeck_status = "", "FAILED_TO_RAISE"
            logger.error(
                "Guest eCheck FAILED to raise for order %s (%.2f): %s",
                order.order_number, float(order.total), _ach_exc, exc_info=True,
            )
        try:
            await db.execute(
                _text("UPDATE orders SET qb_echeck_id=:eid, qb_echeck_status=:est WHERE id=:oid"),
                {"eid": _echeck_id or None, "est": _echeck_status, "oid": str(order.id)},
            )
        except Exception as _save_exc:
            logger.warning("Could not save eCheck state on guest order %s: %s", order.order_number, _save_exc)

        from app.services.qb_payments_service import ECHECK_NOT_COLLECTED as _NOT_COLLECTED
        if _echeck_status in _NOT_COLLECTED:
            try:
                from app.services.email_service import EmailService as _ES
                _svc = _ES(db)
                for _to in _svc._business_inboxes():
                    _svc.send_raw(
                        to_email=_to,
                        subject=f"Bank transfer not collected — order {order.order_number}",
                        body_html=(
                            f"<p>Guest order <strong>{order.order_number}</strong> for "
                            f"<strong>${float(order.total):.2f}</strong> was placed by bank "
                            f"transfer, but no money is being collected — QuickBooks reported "
                            f"<strong>{_echeck_status}</strong>.</p>"
                            f"<p>The order is fine. Please contact the customer to arrange payment.</p>"
                        ),
                    )
            except Exception as _mail_exc:
                logger.warning("Could not alert the office about the failed guest eCheck: %s", _mail_exc)

    # 6. Create OrderItem records + deduct inventory
    from sqlalchemy import update as _update

    _variant_ids_to_sync: list[str] = []
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

        # Whatever the shelves could not cover stays owed, written as negative
        # stock so the shortfall is visible and the next receipt pays it down.
        if qty_to_deduct > 0 and item_data.get("is_backordered"):
            _recs = (await db.execute(
                select(InventoryRecord)
                .where(InventoryRecord.variant_id == item_data["variant_id"])
                .order_by(InventoryRecord.quantity.desc())
            )).scalars().all()
            if _recs:
                await db.execute(
                    _update(InventoryRecord)
                    .where(InventoryRecord.id == _recs[0].id)
                    .values(quantity=InventoryRecord.quantity - qty_to_deduct)
                )

        # Collect this variant for a single batched QB sync after the loop
        # (dedup so a variant is never synced twice within one order).
        _vid = str(item_data["variant_id"])
        if _vid not in _variant_ids_to_sync:
            _variant_ids_to_sync.append(_vid)

    # Stock is now out of inventory for this order — mark it so a later
    # cancel/delete returns exactly this stock to the shelf.
    order.inventory_deducted = True

    # Sync all updated stock to QB in ONE batched task instead of one task per
    # variant. countdown=15 keeps the original buffer so the DB commit lands first.
    # Deliberately NOT pushing QtyOnHand to QuickBooks — this order's QB invoice
    # reduces the quantity there and books COGS. See order_service.create_order.
    logger.info(
        "Guest order %s deducted %d variant(s) locally — QB follows the invoice",
        order.order_number, len(_variant_ids_to_sync),
    )

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
        # Invoice only, by request — see SEND_ORDER_CONFIRMATION_EMAIL.
        from app.core.config import settings as _cfg_email
        if _cfg_email.SEND_ORDER_CONFIRMATION_EMAIL:
            _email_svc.send_order_confirmation(order, order.guest_email, restock_dates=await _restock_dates_for_order(order, db))
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


async def _restock_dates_for_order(order, db) -> dict:
    """When the next purchase order covering each backordered line is due.

    Read here rather than inside the email service, which is synchronous and
    cannot query. Returns {} on any failure — a confirmation email must go out
    even if the date cannot be found, since the alternative is the customer
    hearing nothing at all.
    """
    try:
        from sqlalchemy import func as _f, select as _s
        from app.models.purchase_order import POLineItem as _L, PurchaseOrder as _P
        ids = [
            str(i.variant_id) for i in (getattr(order, "items", []) or [])
            if getattr(i, "is_backordered", False) and getattr(i, "variant_id", None)
        ]
        if not ids:
            return {}
        return {
            str(vid): d
            for vid, d in (await db.execute(
                _s(_L.product_variant_id, _f.min(_P.expected_delivery))
                .join(_P, _P.id == _L.po_id)
                .where(_L.product_variant_id.in_(ids))
                .where(_P.expected_delivery.isnot(None))
                .where(_P.status.notin_(["cancelled", "received"]))
                .group_by(_L.product_variant_id)
            )).all()
        }
    except Exception:
        return {}
