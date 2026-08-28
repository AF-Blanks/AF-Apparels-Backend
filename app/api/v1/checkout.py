import logging
import traceback
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ForbiddenError, PaymentError, ValidationError
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
    except (ForbiddenError, PaymentError, ValidationError, HTTPException):
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

    # Validate: at least one payment method supplied
    has_qb     = bool(payload.qb_token or payload.saved_card_id)
    has_stripe = bool(payload.payment_intent_id)
    has_ach    = payload.payment_method == "ach"
    has_net30  = payload.payment_method == "net_30"  # wholesale invoice/NET 30 — no upfront charge
    has_net7   = payload.payment_method == "net_7"   # wholesale invoice/NET 7 — no upfront charge
    if not has_qb and not has_stripe and not has_ach and not has_net30 and not has_net7:
        raise ValidationError(
            "Payment required: supply qb_token, saved_card_id, payment_intent_id, "
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
    if has_ach:
        from app.services.qb_payments_service import QBPaymentsService as _QBPaySvc

        if not payload.ach_authorized:
            raise ValidationError(
                "Please authorise the bank transfer before placing the order."
            )
        _ach_acct = "".join(c for c in (payload.ach_account_number or "") if c.isdigit())
        if len(_ach_acct) < 4:
            raise ValidationError("Please enter your full bank account number.")
        if not _QBPaySvc.routing_number_is_valid(payload.ach_routing_number):
            raise ValidationError(
                "That routing number doesn't look right — please check the nine digits."
            )
        if not (payload.ach_first_name or "").strip() or not (payload.ach_last_name or "").strip():
            raise ValidationError(
                "Please enter the first and last name on the bank account."
            )

    discount_percent = getattr(request.state, "tier_discount_percent", Decimal("0"))
    group_id = getattr(request.state, "discount_group_id", None)

    # ── QB Payments flow ──────────────────────────────────────────────────────
    qb_charge_id: str | None = None
    qb_payment_status: str | None = None
    coupon_discount_dc = None
    coupon_discount_amount = Decimal("0")

    if has_qb:
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
        _convenience_fee_dc = (cart.subtotal * Decimal("0.03")).quantize(Decimal("0.01")) if _account_type == "wholesale" else Decimal("0.00")
        total_float = float(cart.subtotal + base_shipping + expedited_surcharge + tax_amount_dc - coupon_discount_amount + _convenience_fee_dc)

        qb_pay = QBPaymentsService()
        try:
            if payload.saved_card_id:
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

        qb_charge_id = charge_resp.get("id")
        qb_payment_status = charge_resp.get("status", "UNKNOWN")

        # The charge API call not raising an exception only means QuickBooks
        # accepted the request — it doesn't mean the card was approved. A
        # declined/errored charge must never result in an order: without
        # this check, a declined card still produced a fully "paid" order
        # (confirmed, inventory deducted, invoice+payment synced to QB) with
        # no money actually collected. charge_card/charge_saved_card capture by
        # default, so a successful charge returns status "CAPTURED"; any other
        # status (DECLINED, etc.) aborts here before an order is created.
        if qb_payment_status != "CAPTURED":
            raise PaymentError(
                f"Payment was not approved (status: {qb_payment_status}). "
                "Please check your card details or try a different payment method."
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

    # ── Bank debit ────────────────────────────────────────────────────────────
    # Raised against the order's own total rather than a figure worked out again
    # here, so what leaves the customer's bank is always exactly what the invoice
    # says. A card is charged before the order exists because the money is either
    # there or it is not; a bank debit clears over days and can still be returned,
    # so there is nothing to wait for and no reason to hold the order back.
    if has_ach:
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

        if _echeck_status == "FAILED_TO_RAISE":
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
                            f"but QuickBooks would not accept the debit.</p>"
                            f"<p>The order is fine — nothing has been collected. "
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
            _sel(_Order).options(_sil(_Order.items)).where(_Order.id == order.id)
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
                _email_svc.send_admin_new_order_alert(_order_full)
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
