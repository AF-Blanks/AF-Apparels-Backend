import asyncio
import logging
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ConflictError, ForbiddenError, PaymentError, ValidationError
from app.schemas.order import CheckoutConfirmRequest, CreatePaymentIntentRequest, OrderOut
from app.services.backorder_rules import MixedOrderError
from app.services.cart_service import CartService
from app.services.order_service import OrderService
from app.services.payment_service import PaymentService
from app.api.v1.discounts import validate_discount_code, compute_discount_amount
from app.models.discount import DiscountUsage

_log = logging.getLogger(__name__)
router = APIRouter(prefix="/checkout", tags=["checkout"])


# ── Stripe: create payment intent ─────────────────────────────────────────────

@router.post("/intent")
async def create_payment_intent(
    payload: CreatePaymentIntentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create Stripe PaymentIntent for current cart total."""
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    discount_percent = getattr(request.state, "tier_discount_percent", Decimal("0"))
    cart_svc = CartService(db)
    cart = await cart_svc.get_cart_with_pricing(company_id, discount_percent)

    if not cart.items:
        raise ValidationError("Cart is empty")

    total = cart.subtotal + cart.validation.estimated_shipping
    payment_svc = PaymentService(db)
    intent = await payment_svc.create_payment_intent(
        amount_decimal=total,
        metadata={"company_id": str(company_id)},
    )

    return {
        "client_secret": intent.client_secret,
        "payment_intent_id": intent.id,
        "amount": total,
    }


# ── QB Payments: server-side tokenize ────────────────────────────────────────

@router.post("/tokenize")
async def tokenize_card(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Tokenize raw card data via QB Payments API and auto-save card to QB customer wallet.

    Expected payload: { card: { number, expMonth, expYear, cvc, name (opt), address: { postalCode } (opt) } }
    Returns: { "token": "<qb_one_time_token>" }

    ⚠ Production recommendation: use QB.js on the client to tokenize and skip
    this endpoint — it reduces PCI scope to SAQ A.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    company_id = getattr(request.state, "company_id", None)
    _log.info("tokenize_card called — company: %s (card save runs here, not at confirm)", company_id)

    from app.services.qb_payments_service import QBPaymentsService
    qb_pay = QBPaymentsService()
    try:
        card = payload["card"]
        token = qb_pay.create_token(
            card_number=card["number"],
            exp_month=card["expMonth"],
            exp_year=card["expYear"],
            cvc=card["cvc"],
            name=card.get("name"),
            postal_code=card.get("address", {}).get("postalCode"),
        )
    except KeyError as exc:
        raise ValidationError(f"Missing required card field: {exc}") from exc
    except RuntimeError as exc:
        raise ValidationError(str(exc)) from exc

    # Auto-save card to QB customer wallet — wholesale accounts only
    if not company_id:
        return {"token": token}

    try:
        from sqlalchemy import select as _select
        from app.models.company import Company as _Company
        company = (await db.execute(
            _select(_Company).where(_Company.id == company_id)
        )).scalar_one_or_none()
        _log.info("Card save attempt — company: %s, qb_customer_id: %s", company_id, company.qb_customer_id if company else None)
        if company:
            # QB Payments customer ID is always str(company_id) — derive directly,
            # never write to company.qb_customer_id (that column is for QB Accounting).
            qb_payments_cust_id = qb_pay.create_customer(str(company_id))
            _log.info("QB Payments customer ready: %s", qb_payments_cust_id)
            if qb_payments_cust_id:
                saved = qb_pay.save_card(
                    customer_id=qb_payments_cust_id,
                    card_number=card["number"],
                    exp_month=card["expMonth"],
                    exp_year=card["expYear"],
                    cvc=card["cvc"],
                    name=card.get("name"),
                )
                _log.info("Card save SUCCESS for company %s — card_id: %s", company_id, saved.get("id"))
                if saved.get("id") and not company.default_payment_method_id:
                    company.default_payment_method_id = saved["id"]
                await db.commit()
    except Exception as _exc:
        _log.warning("Card save FAILED for company %s: %s: %s", company_id, type(_exc).__name__, _exc)

    return {"token": token}


# ── Confirm order (QB Payments or Stripe) ─────────────────────────────────────

@router.post("/confirm", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def confirm_checkout(
    payload: CheckoutConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create order after payment authorisation.

    Supports two payment flows:
    - QB Payments: provide qb_token (one-time) or saved_card_id.
    - Stripe (legacy): provide payment_intent_id.

    Note: card auto-save happens at POST /checkout/tokenize (not here).
    """
    try:
        return await _confirm_checkout_inner(payload, request, db)
    except (ForbiddenError, PaymentError, ValidationError, MixedOrderError, HTTPException):
        raise  # let framework handle these as-is
    except Exception as exc:
        _log.exception("confirm_checkout UNHANDLED ERROR — payload fields: %s", getattr(payload, "__fields_set__", None))
        raise HTTPException(
            status_code=500,
            detail=f"Order creation failed: {type(exc).__name__}: {exc}",
        ) from exc


#: Stripe's own limits on a metadata bag: 50 keys, keys of 40 characters,
#: values of 500. Staying well inside them is what stops a large order having
#: its payment rejected over a label.
_META_VALUE_MAX = 500
_META_ITEM_KEYS = 10


def _stripe_item_summary(items) -> dict[str, str]:
    """What was actually sold, in a shape Stripe will accept.

    A payment of $118,687.46 sitting in the dashboard with nothing but a company
    name against it is no use to anybody reconciling it. This puts the lines
    themselves on the payment — product, colour/size, quantity — packed into as
    few metadata keys as the 500-character limit allows, so a 38-line order is
    readable rather than truncated to its first three rows.

    Never lets the summary be the reason a charge fails: anything that will not
    fit is dropped and counted, and any error here returns an empty bag rather
    than raising.
    """
    try:
        lines: list[str] = []
        units = 0
        for it in (items or []):
            qty = int(getattr(it, "quantity", 0) or 0)
            units += qty
            name = (getattr(it, "product_name", "") or "").strip()
            colour = (getattr(it, "color", "") or "").strip()
            size = (getattr(it, "size", "") or "").strip()
            variant = "/".join(p for p in (colour, size) if p)
            label = f"{name} {variant}".strip() if variant else name
            lines.append(f"{label} x{qty}" if label else f"x{qty}")

        meta: dict[str, str] = {
            "order_lines": str(len(lines)),
            "total_units": str(units),
        }

        # Pack the lines into as few keys as they fit in, in order.
        keys_used, buf, dropped = 0, "", 0
        for i, line in enumerate(lines):
            piece = line if not buf else f"; {line}"
            if len(buf) + len(piece) <= _META_VALUE_MAX:
                buf += piece
                continue
            keys_used += 1
            meta[f"items_{keys_used}"] = buf
            if keys_used >= _META_ITEM_KEYS:
                dropped = len(lines) - i
                buf = ""
                break
            buf = line
        if buf:
            keys_used += 1
            meta[f"items_{keys_used}"] = buf
        if dropped:
            meta["items_truncated"] = f"+{dropped} more lines — see the order"
        return meta
    except Exception:  # noqa: BLE001 — a label must never cost a payment
        return {}


async def _charge_via_stripe(
    *, payload, request, db, company_id, amount: float, items=None,
) -> dict:
    """Take the money through Stripe and answer in the shape the rest expects.

    Returns {"id", "status"} where status is "CAPTURED" when the money is in —
    the same word the QuickBooks path uses — so everything downstream reads one
    vocabulary rather than branching on who was paid.

    A bank debit never returns CAPTURED here. It leaves Stripe in `processing`
    and settles days later, so it comes back as PENDING and the order is placed
    unpaid; the webhook marks it paid when the money actually lands. Treating a
    bank debit as collected at checkout is how goods go out against money that
    never arrives.
    """
    from sqlalchemy import select as _sel
    from app.models.company import Company as _Co
    import stripe

    from app.services.stripe_service import (
        StripeNotConfigured, StripeService,
    )

    svc = StripeService()
    company = (await db.execute(_sel(_Co).where(_Co.id == company_id))).scalar_one_or_none()

    # The same key that guards the attempt guards the charge. A retry of one
    # press reuses it, so Stripe returns the original charge rather than making
    # a second — the last line of defence if both earlier layers are bypassed.
    _key = (payload.attempt_key or "").strip() or f"order:{company_id}:{amount:.2f}"

    _meta = {
        "company_id": str(company_id),
        "company_name": (company.name if company else "") or "",
        "ip_address": _client_ip(request),
        "user_agent": (request.headers.get("user-agent") or "")[:200],
    }
    _meta.update(_stripe_item_summary(items))

    # The line shown in the dashboard's payment list, where there is room for
    # one line only — so it carries the shape of the order, not its contents.
    _desc = f"AF Apparels — {(company.name if company else None) or company_id}"
    if _meta.get("order_lines"):
        _desc += f" — {_meta['order_lines']} lines, {_meta['total_units']} pcs"

    try:
        cust_id = None
        if company:
            cust_id = company.stripe_customer_id
            if not cust_id:
                cust_id = await asyncio.to_thread(
                    lambda: svc.find_or_create_customer(
                        company_id=str(company_id),
                        name=company.name or f"Company {company_id}",
                        email=company.company_email,
                    )
                )
                company.stripe_customer_id = cust_id
                await db.commit()

        # ── a bank debit ────────────────────────────────────────────────
        if payload.payment_method == "ach":
            if not payload.ach_authorized:
                raise ValidationError(
                    "Please tick the box authorising us to debit your bank account."
                )
            if not payload.stripe_payment_method_id:
                raise ValidationError(
                    "Bank details are missing. Please re-enter them and try again."
                )
            out = await asyncio.to_thread(
                lambda: svc.charge_bank_account(
                    amount=amount,
                    payment_method_id=payload.stripe_payment_method_id,
                    idempotency_key=f"ach:{_key}",
                    customer_id=cust_id,
                    description=_desc,
                    metadata=_meta,
                )
            )
        # ── a card the customer already saved ───────────────────────────
        elif payload.stripe_saved_method_id:
            if not cust_id:
                raise ValidationError(
                    "No saved cards on this account yet. Please enter a card."
                )
            out = await asyncio.to_thread(
                lambda: svc.charge_saved_card(
                    amount=amount,
                    customer_id=cust_id,
                    payment_method_id=payload.stripe_saved_method_id,
                    idempotency_key=f"card:{_key}",
                    description=_desc,
                    metadata=_meta,
                )
            )
        # ── a card entered now ──────────────────────────────────────────
        elif payload.stripe_payment_method_id:
            out = await asyncio.to_thread(
                lambda: svc.charge_card(
                    amount=amount,
                    payment_method_id=payload.stripe_payment_method_id,
                    idempotency_key=f"card:{_key}",
                    customer_id=cust_id,
                    description=_desc,
                    save_for_future=bool(payload.save_card),
                    metadata=_meta,
                )
            )
        else:
            raise ValidationError(
                "Payment details are missing. Please enter a card or bank account."
            )

    except StripeNotConfigured as exc:
        # The switch was thrown before the keys were in. Say so plainly rather
        # than letting a customer meet a blank failure.
        _log.critical("Stripe is the active provider but has no keys: %s", exc)
        raise PaymentError(
            "Card payments are temporarily unavailable. Please call us on "
            "214-272-7213 and we will take the order."
        )
    except ValidationError:
        raise
    except stripe.IdempotencyError as exc:
        # The same attempt key came back with different details — a cart edited
        # in another tab, most often. Stripe's own wording here is written for
        # developers ("Keys for idempotent requests can only be used with the
        # same parameters…") and means nothing to a customer.
        _log.warning("Stripe idempotency clash for company %s: %s", company_id, exc)
        raise PaymentError(
            "Something changed since you opened this page. Please refresh and "
            "enter your payment details again — nothing has been charged."
        )
    except Exception as exc:  # noqa: BLE001 — Stripe's own errors, made readable
        _msg = getattr(exc, "user_message", None) or str(exc)
        _log.warning("Stripe charge failed for company %s: %s", company_id, _msg)
        raise PaymentError(
            f"Payment was not approved: {_msg}. Please check your details or "
            "try a different payment method."
        )

    # ── translate into the vocabulary the rest of checkout speaks ───────
    if out["payment_status"] == "paid":
        status = "CAPTURED"
    elif out["payment_status"] == "pending":
        # A bank debit under way, or a card asking the cardholder to
        # authenticate. Neither is money in hand.
        status = "PENDING"
    else:
        status = (out.get("status") or "FAILED").upper()

    if status not in ("CAPTURED", "PENDING"):
        raise PaymentError(
            out.get("failure_message")
            or "Payment was not approved. Please check your details or try another card."
        )

    return {
        "id": out["id"],
        "status": status,
        "_stripe": out,
    }


def _client_ip(request) -> str:
    """The customer's address, for the bank debit mandate.

    Behind a proxy the socket address is the proxy's; the first hop in
    X-Forwarded-For is the one that belongs to the person who agreed.
    """
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (getattr(request.client, "host", "") or "0.0.0.0")


async def _confirm_checkout_inner(
    payload: CheckoutConfirmRequest,
    request: Request,
    db: AsyncSession,
):
    """One press of Pay, however many times it arrives.

    Everything below charges money and creates an order. If the customer
    double-clicks, or their phone drops the reply and the browser retries, this
    runs twice — and without the guard, charges twice. See payment_attempt.py
    for why three layers are needed rather than one.

    A client that sends no attempt_key gets exactly the old behaviour, guard and
    all bypassed. That is how the QuickBooks path runs today; nothing changes
    for it until it starts sending one.
    """
    from app.services import payment_attempt as _attempt

    _key = (payload.attempt_key or "").strip()[:120]
    if not _key:
        return await _do_confirm_checkout(payload, request, db)

    _company_for_attempt = getattr(request.state, "company_id", None)
    try:
        await _attempt.claim(db, attempt_key=_key, company_id=str(_company_for_attempt or ""))
    except _attempt.AttemptAlreadyDone as done:
        # They already paid and already have an order. Give them that one back
        # rather than an error for something that worked.
        _log.info("checkout attempt %s already completed as order %s", _key, done.order_id)
        return await _order_out_by_id(done.order_id, db)
    except _attempt.AttemptInFlight:
        raise ConflictError(
            "This order is already being placed — give it a moment rather than "
            "paying again. If nothing happens, refresh and check your orders "
            "before retrying."
        )

    try:
        result = await _do_confirm_checkout(payload, request, db)
    except (PaymentError, ValidationError, MixedOrderError, ForbiddenError) as exc:
        # Nothing was collected, or nothing should have been. Free the key so
        # fixing the problem and pressing Pay again is not refused as a repeat.
        await _attempt.release(db, attempt_key=_key)
        raise
    except Exception as exc:  # noqa: BLE001
        # Something broke after money may already have moved. The key stays
        # claimed and marked failed, so a blind retry cannot charge again — a
        # person looks at it instead.
        await _attempt.failed(db, attempt_key=_key, reason=str(exc))
        raise

    await _attempt.completed(
        db, attempt_key=_key, order_id=str(result.id),
        payment_reference=getattr(result, "qb_payment_charge_id", None),
    )
    return result


async def _order_out_by_id(order_id: str, db: AsyncSession):
    """Re-read an order for a retry that already succeeded."""
    from sqlalchemy import select as _sel
    from sqlalchemy.orm import selectinload as _sel_in
    from app.models.order import Order as _Order

    order = (await db.execute(
        _sel(_Order).options(_sel_in(_Order.items)).where(_Order.id == order_id)
    )).scalar_one_or_none()
    if not order:
        raise ValidationError("That order could not be found. Please check your orders.")
    return OrderOut.model_validate(order)


async def _do_confirm_checkout(
    payload: CheckoutConfirmRequest,
    request: Request,
    db: AsyncSession,
):
    from app.core.config import settings as _cfg_maint
    if _cfg_maint.MAINTENANCE_MODE and not (
        getattr(request.state, "is_admin", False)
        or getattr(request.state, "is_staff", False)
    ):
        raise ValidationError(
            "The store is briefly closed for maintenance and cannot take orders "
            "right now. Please try again shortly, or email info@afblanks.com and "
            "we will place it for you."
        )
    # Staff are let through on purpose. Maintenance mode exists to keep customers
    # out while something is being changed underneath them — and the person doing
    # the changing is usually the one who needs to place an order to see whether
    # it worked. Closing the shop to them as well leaves no way to check without
    # reopening it to everyone. Guests are still refused: guest.py has no staff
    # to recognise.

    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    _account_type = getattr(request.state, "account_type", "wholesale")
    if not company_id:
        raise ForbiddenError("Company account required")

    _log.info(
        "confirm_checkout called — company: %s, fields_set: %s",
        company_id,
        payload.__fields_set__,
    )
    _log.info(
        "confirm_checkout payment — qb_token: %s, saved_card_id: %s, payment_intent_id: %s",
        bool(payload.qb_token),
        bool(payload.saved_card_id),
        bool(payload.payment_intent_id),
    )

    # Validate: at least one payment method supplied.
    #
    # has_stripe used to mean payment_intent_id alone — a field left over from an
    # older Stripe flow that nothing sends any more. The form sends a payment
    # method id now, so a customer could fill in a Stripe card, reach this line,
    # and be told to supply a QuickBooks token.
    has_qb     = bool(payload.qb_token or payload.saved_card_id)
    has_stripe = bool(
        payload.stripe_payment_method_id
        or payload.stripe_saved_method_id
        or payload.payment_intent_id
    )
    has_ach    = payload.payment_method == "ach"
    has_net30  = payload.payment_method == "net_30"  # wholesale invoice/NET 30 — no upfront charge
    has_net7   = payload.payment_method == "net_7"   # wholesale invoice/NET 7 — no upfront charge
    if not has_qb and not has_stripe and not has_ach and not has_net30 and not has_net7:
        raise ValidationError(
            "Payment required: supply card or bank details, "
            "payment_method=ach, payment_method=net_30, or payment_method=net_7"
        )

    # Validate the requested credit term is actually enabled for this company.
    if has_net30 or has_net7:
        from sqlalchemy import select as _sel
        from app.models.company import Company as _Company
        _company = (await db.execute(
            _sel(_Company).where(_Company.id == company_id)
        )).scalar_one_or_none()
        _term_field = "net30_enabled" if has_net30 else "net7_enabled"
        _term_name = "Net 30" if has_net30 else "Net 7"
        if not _company or not getattr(_company, _term_field, False):
            raise ValidationError(f"{_term_name} payment terms are not available for your account. Contact AF Apparels to request it.")

    # Bank details are checked before anything is written down. A mistyped
    # routing number is the common failure, and catching it here means the
    # customer is told to fix it rather than ending up with an order whose
    # payment could never have been raised.
    # These are QuickBooks' fields, and only QuickBooks needs them: it is handed
    # the raw routing and account numbers and raises the debit itself. On the
    # Stripe path the customer's bank details were collected by Stripe and never
    # reach us — there is a payment method id and nothing else — so demanding an
    # account number here refused every Stripe bank transfer for want of a number
    # we deliberately do not hold.
    from app.services.stripe_service import is_stripe_active as _stripe_on_val

    if has_ach and not _stripe_on_val():
        from app.services.qb_payments_service import QBPaymentsService as _QBPaySvc

        if not payload.ach_authorized:
            raise ValidationError(
                "Please authorise the bank transfer before placing the order."
            )
        _ach_acct = "".join(c for c in (payload.ach_account_number or "") if c.isdigit())
        if len(_ach_acct) < 4:
            raise ValidationError("Please enter your full bank account number.")
        # Both declined debits carried an account of nothing but zeros, beside a
        # routing number of nine of them. Neither is an account anyone can be
        # paid from.
        if len(set(_ach_acct)) == 1:
            raise ValidationError(
                "That account number doesn't look right — please check it."
            )
        if not _QBPaySvc.routing_number_is_valid(payload.ach_routing_number):
            raise ValidationError(
                "That routing number doesn't look right — please check the nine digits."
            )
        if not (payload.ach_first_name or "").strip() or not (payload.ach_last_name or "").strip():
            raise ValidationError(
                "Please enter the first and last name on the bank account."
            )

    elif has_ach:
        # Stripe holds the bank details; what it cannot hold is the customer's
        # permission to take the money, and that is still ours to insist on.
        if not payload.ach_authorized:
            raise ValidationError(
                "Please authorise the bank transfer before placing the order."
            )
        if not payload.stripe_payment_method_id:
            raise ValidationError(
                "Bank details are missing. Please re-enter them and try again."
            )

    discount_percent = getattr(request.state, "tier_discount_percent", Decimal("0"))
    group_id = getattr(request.state, "discount_group_id", None)

    # An order ships once, so it cannot be part on the shelf and part owed. The
    # order writer refuses such a basket too, and is the last word on it — but by
    # then the card has been charged, so it is asked here first, while nothing has
    # been taken. Every payment method passes through this point.
    from sqlalchemy import select as _sel_bo
    from app.models.order import CartItem as _CartItem_bo
    from app.models.product import Product as _Product_bo, ProductVariant as _Variant_bo
    from app.services.backorder_rules import (
        backorder_flags as _bo_flags,
        check_not_mixed as _bo_check,
        describe_line as _bo_label,
    )

    _bo_rows = (await db.execute(
        _sel_bo(_CartItem_bo, _Variant_bo, _Product_bo)
        .join(_Variant_bo, _Variant_bo.id == _CartItem_bo.variant_id)
        .join(_Product_bo, _Product_bo.id == _Variant_bo.product_id)
        .where(_CartItem_bo.company_id == company_id)
    )).all()
    if _bo_rows:
        _bo_map = await _bo_flags(db, {ci.variant_id: ci.quantity for ci, _v, _p in _bo_rows})
        _bo_check([
            {
                "label": _bo_label(prod.name, var.color, var.size, var.sku),
                "backordered": _bo_map.get(ci.variant_id, False),
            }
            for ci, var, prod in _bo_rows
        ])

    # ── Taking money up front ─────────────────────────────────────────────────
    qb_charge_id: str | None = None
    qb_payment_status: str | None = None
    coupon_discount_dc = None
    coupon_discount_amount = Decimal("0")

    # Set before the block, because the block does not always run. An order on
    # terms, or a QuickBooks bank debit, is created without a charge and reaches
    # the lines below with nothing having been assigned here — which is how
    # `_payment_provider` came to be read before it existed.
    _payment_provider = "quickbooks"
    charge_resp: dict | None = None

    # This block is where a card is charged before the order is written, and it
    # was gated on has_qb — "a QuickBooks token or saved card was supplied".
    # Stripe supplies neither, so on the Stripe path the whole block was skipped:
    # no charge was raised at all, and the order fell through to code expecting
    # one. It runs for either provider now; which of them actually moves the
    # money is decided inside, by the same switch as everywhere else.
    if has_qb or has_stripe:
        from app.services.cart_service import CartService as _CartService
        from app.services.qb_payments_service import QBPaymentsService

        cart_svc = _CartService(db)
        cart = await cart_svc.get_cart_with_pricing(company_id, discount_percent, group_id)
        if not cart.items:
            raise ValidationError("Cart is empty")

        if payload.shipping_method == "will_call":
            base_shipping = Decimal("0.00")
            expedited_surcharge = Decimal("0.00")
        elif payload.shipping_method == "free":
            # Per-customer free shipping — server-verify the cart actually
            # qualifies before honoring $0 (blocks a tampered "free" selection).
            from sqlalchemy import select as _sel_ship
            from app.models.company import Company as _Company_ship
            _co_ship = (await db.execute(
                _sel_ship(_Company_ship).where(_Company_ship.id == company_id)
            )).scalar_one_or_none()
            if (not _co_ship or not _co_ship.ship_free_enabled
                    or cart.subtotal < Decimal(str(_co_ship.ship_free_min or 0))):
                raise ValidationError("Free shipping is not available for this order.")
            base_shipping = Decimal("0.00")
            expedited_surcharge = Decimal("0.00")
        elif payload.shipping_method == "pallet":
            # Pallet flat rate — server-verify it is enabled for this company and
            # the amount is one of its configured rates (Dallas/Houston/Other).
            # Never trust an arbitrary client value for the charge.
            from sqlalchemy import select as _sel_ship
            from app.models.company import Company as _Company_ship
            _co_ship = (await db.execute(
                _sel_ship(_Company_ship).where(_Company_ship.id == company_id)
            )).scalar_one_or_none()
            _pallet_cost = Decimal(str(payload.shipping_cost or 0))
            _rates = [
                Decimal(str(_co_ship.ship_pallet_dallas or 0)),
                Decimal(str(_co_ship.ship_pallet_houston or 0)),
                Decimal(str(_co_ship.ship_pallet_other or 0)),
            ] if _co_ship else []
            # Valid = a whole multiple of one configured rate (N pallets × rate).
            _valid = any(r > 0 and _pallet_cost >= r and (_pallet_cost % r == 0) for r in _rates)
            if (not _co_ship or not _co_ship.ship_pallet_enabled or not _valid):
                raise ValidationError("Pallet shipping is not available for this order.")
            base_shipping = _pallet_cost
            expedited_surcharge = Decimal("0.00")
        else:
            base_shipping = Decimal(str(payload.shipping_cost)) if payload.shipping_cost else cart.validation.estimated_shipping
            expedited_surcharge = Decimal("45.00") if payload.shipping_method == "expedited" else Decimal("0")

            # Guard against a tampered/near-zero client-supplied shipping cost.
            # A full server-side recompute would need address resolution moved
            # earlier (it currently happens inside create_order, after the
            # card is already charged) — too invasive to restructure safely
            # right now. This at least blocks the crude "shipping_cost: 0.01"
            # tampering pattern.
            if base_shipping < Decimal("1.00"):
                raise ValidationError("Invalid shipping cost")

        # Validate and apply discount code if provided
        if payload.discount_code:
            cart_total_for_coupon = float(cart.subtotal)  # discount applies to subtotal only, not shipping
            coupon_discount_dc, coupon_error = await validate_discount_code(
                payload.discount_code,
                cart_total_for_coupon,
                user_id,
                "wholesale",
                db,
            )
            if coupon_error:
                raise ValidationError(f"Discount code invalid: {coupon_error}")
            coupon_discount_amount = Decimal(str(
                compute_discount_amount(coupon_discount_dc, cart_total_for_coupon)
            ))

        tax_amount_dc = Decimal(str(payload.tax_amount or 0))
        if tax_amount_dc < 0:
            # $0 is legitimate (tax-exempt companies, no-tax-nexus states) —
            # only a negative value is unambiguously invalid/tampered.
            raise ValidationError("Invalid tax amount")
        # The 3% is a card fee. It was safe to leave the payment method out of
        # this while only QuickBooks card payments reached here — a bank debit
        # skipped this block entirely. Once Stripe's bank transfers started
        # coming through it too, they arrived carrying a card's fee: the customer
        # was shown $5.59 and the debit was raised for $5.74.
        _convenience_fee_dc = (
            (cart.subtotal * Decimal("0.03")).quantize(Decimal("0.01"))
            if _account_type == "wholesale" and not has_ach
            else Decimal("0.00")
        )
        total_float = float(cart.subtotal + base_shipping + expedited_surcharge + tax_amount_dc - coupon_discount_amount + _convenience_fee_dc)

        # ── Who takes the money ───────────────────────────────────────────
        #
        # One switch, PAYMENT_PROVIDER. Everything below this point — the order,
        # the invoice, the emails — is the same either way; only the few lines
        # that actually move money differ. Whoever took it is written onto the
        # order, because a refund months later has to go back through the same
        # provider whatever the setting says by then.
        from app.services.stripe_service import is_stripe_active as _stripe_on

        if _stripe_on():
            charge_resp = await _charge_via_stripe(
                payload=payload, request=request, db=db,
                company_id=company_id, amount=total_float,
                items=cart.items,
            )
            _payment_provider = "stripe"
        else:
            _payment_provider = "quickbooks"
            charge_resp = None

        qb_pay = QBPaymentsService()
        try:
            if _payment_provider == "stripe":
                pass  # already charged above
            elif payload.saved_card_id:
                # Saved card — look up QB customer ID from DB (frontend doesn't need to pass it)
                from sqlalchemy import select as _select
                from app.models.company import Company as _Company
                company = (await db.execute(
                    _select(_Company).where(_Company.id == company_id)
                )).scalar_one_or_none()
                # QB Payments customer ID is always str(company_id)
                qb_cust_id = payload.qb_customer_id or str(company_id)
                if not qb_cust_id:
                    raise ValidationError(
                        "No QB Payments profile found. Complete a checkout with a new card first."
                    )
                charge_resp = qb_pay.charge_saved_card(
                    customer_id=qb_cust_id,
                    card_id=payload.saved_card_id,
                    amount=total_float,
                    description=f"AF Apparels order — company {company_id}",
                )
            else:
                charge_resp = qb_pay.charge_card(
                    token=payload.qb_token,  # type: ignore[arg-type]
                    amount=total_float,
                    description=f"AF Apparels order — company {company_id}",
                )
        except RuntimeError as exc:
            raise ValidationError(f"Payment failed: {exc}") from exc

        # A Stripe id is not a QuickBooks charge id, and must not be written into
        # the column that means one — a refund reading it would go looking for a
        # charge QuickBooks has never heard of. The Stripe ids are recorded on the
        # order in their own columns further down.
        qb_charge_id = None if _payment_provider == "stripe" else charge_resp.get("id")
        qb_payment_status = charge_resp.get("status", "UNKNOWN")

        # The charge API call not raising an exception only means QuickBooks
        # accepted the request — it doesn't mean the card was approved. A
        # declined/errored charge must never result in an order: without
        # this check, a declined card still produced a fully "paid" order
        # (confirmed, inventory deducted, invoice+payment synced to QB) with
        # no money actually collected. charge_card/charge_saved_card capture by
        # default, so a successful charge returns status "CAPTURED"; any other
        # status (DECLINED, etc.) aborts here before an order is created.
        # PENDING is a real answer, not a failure. A bank debit leaves Stripe in
        # `processing` and settles days later; refusing it here would mean Stripe
        # can take cards and nothing else, which is the one thing it was brought
        # in to do. The order is placed unpaid and the webhook settles it.
        if qb_payment_status == "PENDING":
            pass
        elif qb_payment_status != "CAPTURED":
            from app.core.config import settings as _cfg_card

            if not _cfg_card.ALLOW_UNAPPROVED_CARD_CHARGES:
                raise PaymentError(
                    f"Payment was not approved (status: {qb_payment_status}). "
                    "Please check your card details or try a different payment method."
                )
            logging.getLogger(__name__).critical(
                "ALLOW_UNAPPROVED_CARD_CHARGES is on — letting an order through on a "
                "card QuickBooks answered %s. No money was collected. Turn this off.",
                qb_payment_status,
            )

    # ── Create order record ───────────────────────────────────────────────────
    order_svc = OrderService(db)
    order = await order_svc.create_order(
        company_id=company_id,
        user_id=user_id,
        confirm=payload,
        discount_percent=discount_percent,
        qb_charge_id=qb_charge_id,
        qb_payment_status=qb_payment_status,
        coupon_discount_amount=coupon_discount_amount,
        group_id=group_id,
        is_wholesale=_account_type == "wholesale",
    )

    # Who took it and what they called it. Recorded per order because a refund
    # months from now has to go back through the provider that actually took the
    # money, whatever PAYMENT_PROVIDER happens to say by then.
    order.payment_provider = _payment_provider
    if _payment_provider == "stripe" and isinstance(charge_resp, dict):
        _sd = charge_resp.get("_stripe") or {}
        order.stripe_payment_intent_id = _sd.get("id")
        order.stripe_charge_id = _sd.get("charge_id")
        order.stripe_payment_status = _sd.get("status")
        order.stripe_payment_method_id = _sd.get("payment_method_id")
        order.stripe_customer_id = _sd.get("customer_id")
        # A bank debit is days from settling. The order is real and owed; the
        # webhook is what turns it paid, and only when the money lands.
        order.payment_status = "paid" if _sd.get("payment_status") == "paid" else "unpaid"

        # A bank account typed in rather than logged into needs two small
        # deposits confirmed before anything can be taken. Nothing is in flight
        # until that happens, so it goes on the order's own record and to
        # whoever watches the inbox — otherwise it reads as an ordinary transfer
        # on its way and simply never arrives.
        if _sd.get("next_action") == "verify_with_microdeposits":
            _tl_mv = list(order.timeline or [])
            _tl_mv.append({
                "status": "pending",
                "message": (
                    "Bank transfer NOT started — the customer's bank account still "
                    "needs verifying. Stripe has sent two small deposits; the "
                    "transfer only begins once those are confirmed. Nothing has "
                    "been collected."
                ),
                "created_by": "System",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
            order.timeline = _tl_mv
            try:
                from app.core.config import settings as _cfg_mv
                from app.services.email_service import EmailService as _MailMv
                if _cfg_mv.ADMIN_NOTIFICATION_EMAIL:
                    _MailMv(db).send_raw(
                        to_email=_cfg_mv.ADMIN_NOTIFICATION_EMAIL,
                        subject=f"Bank account needs verifying — order {order.order_number}",
                        body_html=(
                            '<div style="font-family:sans-serif;max-width:560px">'
                            '<div style="background:#1B3A5C;padding:20px;'
                            'border-bottom:3px solid #E8242A"><span style="color:#fff;'
                            'font-weight:900;font-size:20px">AF APPARELS</span></div>'
                            '<div style="padding:24px;color:#2A2830;line-height:1.7">'
                            f'<p>Order <strong>{order.order_number}</strong> was placed '
                            f'with a bank transfer, but the account has not been '
                            f'verified yet, so <strong>no money is on its way</strong>.</p>'
                            f'<p>Amount: <strong>${float(order.total):,.2f}</strong></p>'
                            '<p>Stripe has sent two small deposits to the account. The '
                            'customer has to confirm those amounts before the transfer '
                            'starts. Worth a call if the order is urgent.</p>'
                            '</div></div>'
                        ),
                    )
            except Exception as _mv_exc:  # noqa: BLE001
                _log.warning("Could not send the verification alert: %s", _mv_exc)
    await db.commit()

    # ── Bank debit ────────────────────────────────────────────────────────────
    # Raised against the order's own total rather than a figure worked out again
    # here, so what leaves the customer's bank is always exactly what the invoice
    # says. A card is charged before the order exists because the money is either
    # there or it is not; a bank debit clears over days and can still be returned,
    # so there is nothing to wait for and no reason to hold the order back.
    if has_ach and _payment_provider == "stripe":
        # Stripe debited the account before the order was created, above. All
        # that is left is the record of permission, which is filed either way.
        from app.services.ach_authorization import record_authorization as _record_auth
        await _record_auth(db, order.id, request, payload.ach_authorization_text)

    elif has_ach:
        from app.services.ach_authorization import record_authorization as _record_auth
        from app.services.qb_payments_service import QBPaymentsService as _QBPaySvc

        # Filed before the debit is raised: the permission is what makes raising
        # it lawful, so it should not depend on the debit succeeding.
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
            from app.services.qb_payments_service import ECHECK_NOT_COLLECTED as _NC
            if _echeck_status in _NC:
                # QuickBooks answers 201 for a debit it has refused, so nothing
                # here counts as an error and the body was never written down —
                # which left the one place a decline reason could have been
                # found empty. Whatever it sends is logged verbatim now, minus
                # the account number, which is nobody's business in a log file.
                # Why, not just that. QuickBooks puts the reason in the body and
                # its own transaction screen leaves the comment column empty for
                # eChecks whatever description is sent — so this was the one
                # place a decline reason could be read, and it went to a log file
                # nobody opens.
                from app.services.qb_payments_service import (
                    echeck_decline_reason as _why,
                )
                _reason = _why(_echeck)
                _safe = {k: v for k, v in _echeck.items() if k != "bankAccount"}
                _log.error(
                    "eCheck NOT COLLECTED for order %s — id=%s status=%s amount=%.2f"
                    " — QuickBooks said: %s",
                    order.order_number, _echeck_id, _echeck_status, float(order.total),
                    _safe,
                )

                # A line in a log file is not a warning anybody receives. This
                # decline used to leave no mark on the order at all, so the next
                # person to open it saw an ordinary unpaid bank transfer and
                # pressed "Mark as Verified" — recording money that had already
                # been refused. It goes on the order's own timeline now, and to
                # whoever watches the inbox.
                try:
                    _tl = list(order.timeline or [])
                    _tl.append({
                        "status": "payment_failed",
                        "message": (
                            f"Bank transfer was {_echeck_status.lower()} by the bank — "
                            f"no money was collected. Reason: {_reason}. "
                            f"Do not mark this order paid until payment is "
                            f"arranged another way."
                        ),
                        "created_by": "System",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    })
                    order.timeline = _tl
                    await db.commit()
                except Exception as _tl_exc:
                    _log.warning("Could not record the decline on order %s: %s",
                                 order.order_number, _tl_exc)

                try:
                    from app.core.config import settings as _cfg_alert
                    from app.services.email_service import EmailService as _Mail
                    if _cfg_alert.ADMIN_NOTIFICATION_EMAIL:
                        _Mail(db).send_raw(
                            to_email=_cfg_alert.ADMIN_NOTIFICATION_EMAIL,
                            subject=f"Bank transfer declined — order {order.order_number}",
                            body_html=(
                                '<div style="font-family:sans-serif;max-width:560px">'
                                '<div style="background:#1B3A5C;padding:20px;'
                                'border-bottom:3px solid #E8242A">'
                                '<span style="color:#fff;font-weight:900;font-size:20px">'
                                'AF APPARELS</span></div>'
                                '<div style="padding:24px;color:#2A2830;line-height:1.7">'
                                f'<p>The bank transfer for order '
                                f'<strong>{order.order_number}</strong> was '
                                f'<strong>{_echeck_status.lower()}</strong>. '
                                f'No money was collected.</p>'
                                f'<p>Amount: <strong>${float(order.total):,.2f}</strong><br>'
                                f'Reason given: <strong>{_reason}</strong><br>'
                                f'QuickBooks transaction: {_echeck_id}</p>'
                                '<p>The order has been placed and is unpaid. Do not mark '
                                'it as paid — arrange payment another way first.</p>'
                                '</div></div>'
                            ),
                        )
                except Exception as _mail_exc:
                    _log.warning("Could not send the decline alert: %s", _mail_exc)
            else:
                _log.info(
                    "eCheck raised for order %s — id=%s status=%s amount=%.2f",
                    order.order_number, _echeck_id, _echeck_status, float(order.total),
                )
        except Exception as _ach_exc:
            # The order stands: the customer has placed it and nothing about it
            # is wrong. What failed is our attempt to collect, which somebody
            # here has to pick up — so it is recorded on the order and the
            # business is told, rather than shown to the customer as a failure
            # they cannot act on.
            _echeck_id, _echeck_status = "", "FAILED_TO_RAISE"
            _log.error(
                "eCheck FAILED to raise for order %s (%.2f): %s",
                order.order_number, float(order.total), _ach_exc, exc_info=True,
            )

        try:
            from sqlalchemy import text as _t_ach
            await db.execute(
                _t_ach(
                    "UPDATE orders SET qb_echeck_id = :eid, qb_echeck_status = :est"
                    " WHERE id = :oid"
                ),
                {"eid": _echeck_id or None, "est": _echeck_status, "oid": str(order.id)},
            )
        except Exception as _save_exc:
            _log.warning("Could not save eCheck state on order %s: %s", order.order_number, _save_exc)

        # Raised and refused are both "no money is coming", and both need
        # somebody here to pick it up. QuickBooks reports the second as a
        # perfectly successful call, so it has to be read out of the body.
        from app.services.qb_payments_service import ECHECK_NOT_COLLECTED as _NOT_COLLECTED
        if _echeck_status in _NOT_COLLECTED:
            try:
                from app.services.email_service import EmailService as _ES
                _svc = _ES(db)
                for _to in _svc._business_inboxes():
                    _svc.send_raw(
                        to_email=_to,
                        subject=f"Bank transfer could not be started — order {order.order_number}",
                        body_html=(
                            f"<p>Order <strong>{order.order_number}</strong> for "
                            f"<strong>${float(order.total):.2f}</strong> was placed by bank transfer, "
                            f"but no money is being collected — QuickBooks reported "
                            f"<strong>{_echeck_status}</strong>.</p>"
                            + (
                                f"<p>QuickBooks transaction ID: <strong>{_echeck_id}</strong><br>"
                                f"Look it up in Merchant Center &rarr; Activity &amp; Reports &rarr; "
                                f"Transactions, or quote it to Intuit support — they can give the "
                                f"decline reason, which QuickBooks does not return to us.</p>"
                                if _echeck_id else ""
                            )
                            + f"<p>The order is fine — nothing has been collected. "
                            f"Please contact the customer to arrange payment.</p>"
                        ),
                    )
            except Exception as _mail_exc:
                _log.warning("Could not alert the office about the failed eCheck: %s", _mail_exc)

    # Record coupon usage after order is created
    if coupon_discount_dc is not None and coupon_discount_amount > 0:
        usage = DiscountUsage(
            discount_code_id=coupon_discount_dc.id,
            order_id=order.id,
            user_id=user_id,
            discount_amount_applied=coupon_discount_amount,
        )
        db.add(usage)

    # ── Statement transactions ────────────────────────────────────────────────
    # The "charge" line is already created inside OrderService.create_order()
    # (step 12) — adding it again here duplicated every order on the
    # customer's statement (visible as two rows per order, balance doubled).
    # Only the card-payment line is unique to this checkout path.
    from datetime import date as _date
    from uuid import UUID as _UUID
    from app.models.statement import StatementTransaction

    _today = _date.today().isoformat()
    _company_uuid = _UUID(str(company_id))
    _order_total = float(order.total)

    if qb_payment_status == "CAPTURED" and qb_charge_id:
        db.add(StatementTransaction(
            company_id=_company_uuid,
            transaction_date=_today,
            description=f"Card payment for Order {order.order_number}",
            transaction_type="payment",
            amount=_order_total,
            reference_number=qb_charge_id,
            order_id=order.id,
        ))

    await db.commit()

    # ── Send order confirmation email ─────────────────────────────────────────
    try:
        from sqlalchemy import select as _sel
        from sqlalchemy.orm import selectinload as _sil
        from app.models.order import Order as _Order
        from app.models.user import User as _User
        from app.services.email_service import EmailService as _EmailSvc

        _order_full = (await db.execute(
            _sel(_Order)
            .options(_sil(_Order.items), _sil(_Order.company))
            .where(_Order.id == order.id)
        )).scalar_one_or_none()

        if _order_full and user_id:
            _user = (await db.execute(
                _sel(_User).where(_User.id == user_id)
            )).scalar_one_or_none()
            if _user:
                _email_svc = _EmailSvc(db)
                # Invoice only, by request — see SEND_ORDER_CONFIRMATION_EMAIL.
                from app.core.config import settings as _cfg_email
                if _cfg_email.SEND_ORDER_CONFIRMATION_EMAIL:
                    _email_svc.send_order_confirmation(_order_full, _user.email, restock_dates=await _restock_dates_for_order(_order_full, db))
                # Handed over rather than looked up inside: the alert is sent
                # synchronously and cannot load a relationship of its own.
                _co = getattr(_order_full, "company", None)
                _email_svc.send_admin_new_order_alert(
                    _order_full,
                    customer_name=(getattr(_co, "name", None) or "").strip() or None,
                    contact_name=f"{_user.first_name or ''} {_user.last_name or ''}".strip() or None,
                    contact_email=_user.email,
                    contact_phone=(getattr(_co, "phone", None) or getattr(_user, "phone", None)),
                )
    except Exception as _exc:
        _log.warning("Order confirmation email failed: %s", _exc)

    # ── QB invoice sync ───────────────────────────────────────────────────────
    # Redis dedup: checkout.py and webhooks.py both try to fire this for the
    # same order. Only the first one within 120 s wins — prevents double sync.
    try:
        import redis as _redis_sync
        from app.core.config import settings as _cfg
        _r = _redis_sync.Redis.from_url(
            _cfg.REDIS_URL or _cfg.CELERY_BROKER_URL, socket_timeout=2
        )
        _dedup_key = f"qb:order_sync_dispatched:{order.id}"
        if _r.set(_dedup_key, "1", nx=True, ex=120):
            from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
            sync_order_invoice_to_qb.delay(str(order.id))
        else:
            _log.info("QB invoice sync already dispatched for order %s — skipping", order.id)
    except Exception as _exc:
        _log.warning("QB invoice sync dispatch failed: %s", _exc)

    return order


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
