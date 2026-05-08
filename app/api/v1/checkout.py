import logging
import traceback
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ForbiddenError, ValidationError
from app.schemas.order import CheckoutConfirmRequest, CreatePaymentIntentRequest, OrderOut
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
            if not company.qb_customer_id:
                qb_cust_id = qb_pay.create_customer(str(company_id))
                company.qb_customer_id = qb_cust_id
                await db.flush()
                _log.info("QB Payments customer created: %s", qb_cust_id)
            if company.qb_customer_id:
                saved = qb_pay.save_card(
                    customer_id=company.qb_customer_id,
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
    except (ForbiddenError, ValidationError, HTTPException):
        raise  # let framework handle these as-is
    except Exception as exc:
        _log.exception("confirm_checkout UNHANDLED ERROR — payload fields: %s", getattr(payload, "__fields_set__", None))
        raise HTTPException(
            status_code=500,
            detail=f"Order creation failed: {type(exc).__name__}: {exc}",
        ) from exc


async def _confirm_checkout_inner(
    payload: CheckoutConfirmRequest,
    request: Request,
    db: AsyncSession,
):
    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
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

    # Validate: at least one payment method supplied
    has_qb = bool(payload.qb_token or payload.saved_card_id)
    has_stripe = bool(payload.payment_intent_id)
    has_ach = payload.payment_method == "ach"
    if not has_qb and not has_stripe and not has_ach:
        raise ValidationError(
            "Payment required: supply qb_token, saved_card_id, payment_intent_id, or payment_method=ach"
        )

    discount_percent = getattr(request.state, "tier_discount_percent", Decimal("0"))

    # ── QB Payments flow ──────────────────────────────────────────────────────
    qb_charge_id: str | None = None
    qb_payment_status: str | None = None
    coupon_discount_dc = None
    coupon_discount_amount = Decimal("0")

    if has_qb:
        from app.services.cart_service import CartService as _CartService
        from app.services.qb_payments_service import QBPaymentsService

        cart_svc = _CartService(db)
        cart = await cart_svc.get_cart_with_pricing(company_id, discount_percent)
        if not cart.items:
            raise ValidationError("Cart is empty")

        if payload.shipping_method == "will_call":
            base_shipping = Decimal("0.00")
            expedited_surcharge = Decimal("0.00")
        else:
            base_shipping = cart.validation.estimated_shipping
            expedited_surcharge = Decimal("45.00") if payload.shipping_method == "expedited" else Decimal("0")

        # Validate and apply discount code if provided
        if payload.discount_code:
            coupon_discount_dc, coupon_error = await validate_discount_code(
                payload.discount_code,
                float(cart.subtotal),
                user_id,
                "wholesale",
                db,
            )
            if coupon_error:
                raise ValidationError(f"Discount code invalid: {coupon_error}")
            coupon_discount_amount = Decimal(str(
                compute_discount_amount(coupon_discount_dc, float(cart.subtotal))
            ))

        total_float = float(cart.subtotal + base_shipping + expedited_surcharge - coupon_discount_amount)

        qb_pay = QBPaymentsService()
        try:
            if payload.saved_card_id:
                # Saved card — look up QB customer ID from DB (frontend doesn't need to pass it)
                from sqlalchemy import select as _select
                from app.models.company import Company as _Company
                company = (await db.execute(
                    _select(_Company).where(_Company.id == company_id)
                )).scalar_one_or_none()
                qb_cust_id = payload.qb_customer_id or (company.qb_customer_id if company else None)
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

        qb_charge_id = charge_resp.get("id")
        qb_payment_status = charge_resp.get("status", "UNKNOWN")


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
    )

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
    from datetime import date as _date
    from uuid import UUID as _UUID
    from app.models.statement import StatementTransaction

    _today = _date.today().isoformat()
    _company_uuid = _UUID(str(company_id))
    _order_total = float(order.total)

    db.add(StatementTransaction(
        company_id=_company_uuid,
        transaction_date=_today,
        description=f"Order {order.order_number}",
        transaction_type="charge",
        amount=_order_total,
        reference_number=order.order_number,
        order_id=order.id,
    ))

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
            _sel(_Order).options(_sil(_Order.items)).where(_Order.id == order.id)
        )).scalar_one_or_none()

        if _order_full and user_id:
            _user = (await db.execute(
                _sel(_User).where(_User.id == user_id)
            )).scalar_one_or_none()
            if _user:
                _email_svc = _EmailSvc(db)
                _email_svc.send_order_confirmation(_order_full, _user.email)
                _email_svc.send_admin_new_order_alert(_order_full)
    except Exception as _exc:
        _log.warning("Order confirmation email failed: %s", _exc)

    return order
