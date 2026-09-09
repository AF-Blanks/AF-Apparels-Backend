"""Stripe webhook handler with idempotency and event routing."""
import logging

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.order import Order
from app.models.system import WebhookLog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(None, alias="stripe-signature"),
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()
    payload = await request.body()

    # Verify Stripe signature
    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")
    except Exception as exc:
        logger.error("Webhook parse error: %s", exc)
        raise HTTPException(status_code=400, detail="Webhook parse error")

    event_id = event["id"]
    event_type = event["type"]

    # Idempotency check
    existing = await db.execute(
        select(WebhookLog).where(WebhookLog.event_id == event_id)
    )
    if existing.scalar_one_or_none():
        return {"status": "already_processed"}

    # Log event
    log_entry = WebhookLog(
        event_id=event_id,
        event_type=event_type,
        status="processing",
    )
    db.add(log_entry)
    await db.flush()

    try:
        if event_type == "payment_intent.succeeded":
            await _handle_payment_succeeded(db, event["data"]["object"])
        elif event_type == "payment_intent.processing":
            # A bank debit has left. Nothing has arrived and may not for days —
            # this only records that it is on its way, so the order reads as
            # placed-and-owed rather than silently nothing.
            await _handle_payment_processing(db, event["data"]["object"])
        elif event_type == "payment_intent.payment_failed":
            await _handle_payment_failed(db, event["data"]["object"])
        elif event_type == "payment_intent.canceled":
            await _handle_payment_failed(db, event["data"]["object"])
        elif event_type == "charge.refunded":
            await _handle_charge_refunded(db, event["data"]["object"])
        elif event_type == "charge.dispute.created":
            await _handle_dispute(db, event["data"]["object"])
        elif event_type == "mandate.updated":
            # A customer withdrew the authorisation behind a bank debit. Future
            # debits on it will fail; better to know now than on the next order.
            await _handle_mandate_change(db, event["data"]["object"])
        else:
            logger.info("Stripe event %s (%s) received, nothing to do", event_id, event_type)

        log_entry.status = "completed"
        await db.commit()

    except Exception as exc:
        logger.exception("Webhook handler error for event %s: %s", event_id, exc)
        log_entry.status = "failed"
        await db.commit()
        raise HTTPException(status_code=500, detail="Webhook processing failed")

    return {"status": "ok"}


async def _handle_payment_succeeded(db: AsyncSession, payment_intent: dict) -> None:
    intent_id = payment_intent["id"]
    result = await db.execute(
        select(Order).where(Order.stripe_payment_intent_id == intent_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        logger.warning("Order not found for PaymentIntent %s", intent_id)
        return

    # Was this order still waiting on the money? A bank debit settling says yes
    # and is worth telling the customer about; a card was collected and
    # confirmed at checkout, so there is nothing new to say.
    _was_unpaid = (order.payment_status or "").lower() != "paid"

    # A bank debit that settles days later arrives here too, and by then the
    # order has usually moved on — do not drag a shipped order back to
    # "processing" just because the money finally landed.
    _values = {
        "payment_status": "paid",
        "stripe_payment_status": payment_intent.get("status"),
        "amount_paid": (payment_intent.get("amount_received") or 0) / 100,
    }
    if order.status in ("pending", None):
        _values["status"] = "processing"
    _charge = (payment_intent.get("latest_charge")
               or ((payment_intent.get("charges") or {}).get("data") or [{}])[0].get("id"))
    if _charge:
        _values["stripe_charge_id"] = _charge

    await db.execute(update(Order).where(Order.id == order.id).values(**_values))

    # No second confirmation.
    #
    # Checkout already emailed the customer when the order was placed. Sending
    # the same confirmation from here meant every card order arrived twice —
    # and for a bank debit, days later, it re-announced an order the customer
    # placed last week instead of saying the money had landed. A debit that
    # settles is worth a line; a card that was already confirmed is not.
    if _was_unpaid:
        try:
            from app.tasks.email_tasks import send_invoice_email
            send_invoice_email.delay(str(order.id))
        except Exception as _mail_exc:  # noqa: BLE001 — never fail the webhook
            logger.warning(
                "Could not send the payment-received email for order %s: %s",
                order.order_number, _mail_exc,
            )

    # Redis dedup: same key as checkout.py — only one sync fires per order within 120 s.
    try:
        import redis as _redis_sync
        from app.core.config import get_settings as _gs
        _cfg = _gs()
        _r = _redis_sync.Redis.from_url(
            _cfg.REDIS_URL or _cfg.CELERY_BROKER_URL, socket_timeout=2
        )
        _dedup_key = f"qb:order_sync_dispatched:{order.id}"
        if _r.set(_dedup_key, "1", nx=True, ex=120):
            from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
            sync_order_invoice_to_qb.delay(str(order.id))
        else:
            logger.info("QB invoice sync already dispatched for order %s — skipping", order.order_number)
    except Exception as _exc:
        logger.warning("QB invoice sync dispatch (webhook) failed: %s", _exc)

    logger.info("Order %s confirmed via Stripe webhook", order.order_number)


async def _handle_payment_processing(db: AsyncSession, payment_intent: dict) -> None:
    """A bank debit is under way. Owed, not collected."""
    await db.execute(
        update(Order)
        .where(Order.stripe_payment_intent_id == payment_intent["id"])
        .values(payment_status="unpaid", stripe_payment_status="processing")
    )
    logger.info("Stripe bank debit processing — intent=%s", payment_intent["id"])


async def _handle_payment_failed(db: AsyncSession, payment_intent: dict) -> None:
    """Nothing was collected, or a bank debit came back days later refused.

    The second case is the dangerous one: the goods may already have shipped
    against a payment that has now bounced. Marking it failed is not enough by
    itself — somebody has to be told, so this raises it loudly.
    """
    intent_id = payment_intent["id"]
    reason = ((payment_intent.get("last_payment_error") or {}).get("message")
              or payment_intent.get("cancellation_reason") or "unknown")

    order = (await db.execute(
        select(Order).where(Order.stripe_payment_intent_id == intent_id)
    )).scalar_one_or_none()

    await db.execute(
        update(Order)
        .where(Order.stripe_payment_intent_id == intent_id)
        .values(payment_status="failed", stripe_payment_status=payment_intent.get("status"))
    )

    if order and order.status in ("shipped", "delivered"):
        logger.critical(
            "Stripe payment FAILED after the order went out — order=%s intent=%s reason=%s. "
            "Goods have shipped against money that has not arrived.",
            order.order_number, intent_id, reason,
        )
        await _tell_someone(
            db,
            subject=f"Payment failed after shipping — order {order.order_number}",
            body=(
                f"Order {order.order_number} was already {order.status} when its "
                f"payment failed.<br><br>Reason: {reason}<br>"
                f"Amount: ${float(order.total or 0):,.2f}<br><br>"
                "This needs chasing — the goods are out and the money is not in."
            ),
        )
    else:
        logger.warning("Stripe payment failed — intent=%s reason=%s", intent_id, reason)


async def _handle_dispute(db: AsyncSession, dispute: dict) -> None:
    """The customer's bank has pulled the money back and is asking why.

    There is a deadline on answering, and it is short. Nothing here can answer
    it, so the only useful thing is to make sure a person knows today.
    """
    intent_id = dispute.get("payment_intent")
    order = None
    if intent_id:
        order = (await db.execute(
            select(Order).where(Order.stripe_payment_intent_id == intent_id)
        )).scalar_one_or_none()
        await db.execute(
            update(Order)
            .where(Order.stripe_payment_intent_id == intent_id)
            .values(payment_status="failed", stripe_payment_status="disputed")
        )

    where = f"order {order.order_number}" if order else f"payment {intent_id}"
    logger.critical(
        "Stripe dispute opened on %s — amount=%s reason=%s due=%s",
        where, (dispute.get("amount") or 0) / 100,
        dispute.get("reason"), dispute.get("evidence_details", {}).get("due_by"),
    )
    await _tell_someone(
        db,
        subject=f"Chargeback opened — {where}",
        body=(
            f"A dispute has been opened on {where}.<br><br>"
            f"Amount: ${(dispute.get('amount') or 0) / 100:,.2f}<br>"
            f"Reason: {dispute.get('reason')}<br><br>"
            "Evidence has to be submitted in the Stripe dashboard before the "
            "deadline, or the money is lost by default."
        ),
    )


async def _handle_mandate_change(db: AsyncSession, mandate: dict) -> None:
    """A bank debit authorisation was withdrawn."""
    if (mandate.get("status") or "").lower() == "inactive":
        logger.warning(
            "Stripe mandate %s is no longer active — future bank debits on it will fail",
            mandate.get("id"),
        )


async def _tell_someone(db: AsyncSession, *, subject: str, body: str) -> None:
    """Email the admin. Never let a failure here lose the webhook."""
    try:
        from app.core.config import get_settings as _gs
        from app.services.email_service import EmailService
        to = _gs().ADMIN_NOTIFICATION_EMAIL
        if not to:
            return
        EmailService(db).send_raw(
            to_email=to,
            subject=subject,
            body_html=(
                '<div style="font-family:sans-serif;max-width:560px">'
                '<div style="background:#1B3A5C;padding:20px;border-bottom:3px solid #E8242A">'
                '<span style="color:#fff;font-weight:900;font-size:20px">AF APPARELS</span></div>'
                f'<div style="padding:24px;color:#2A2830;line-height:1.7">{body}</div></div>'
            ),
        )
    except Exception as exc:  # noqa: BLE001 — telling someone must never break the webhook
        logger.warning("Could not send payment alert: %s", exc)


async def _handle_charge_refunded(db: AsyncSession, charge: dict) -> None:
    intent_id = charge.get("payment_intent")
    if intent_id:
        await db.execute(
            update(Order)
            .where(Order.stripe_payment_intent_id == intent_id)
            .values(payment_status="refunded", status="refunded")
        )
