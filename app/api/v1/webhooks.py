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
        elif event_type == "payment_intent.payment_failed":
            await _handle_payment_failed(db, event["data"]["object"])
        elif event_type == "charge.refunded":
            await _handle_charge_refunded(db, event["data"]["object"])

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

    await db.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(status="processing", payment_status="paid")
    )

    from app.tasks.email_tasks import send_order_confirmation_email
    from app.tasks.quickbooks_tasks import sync_order_to_qb

    send_order_confirmation_email.delay(str(order.id))
    sync_order_to_qb.delay(str(order.id))

    logger.info("Order %s confirmed via Stripe webhook", order.order_number)


async def _handle_payment_failed(db: AsyncSession, payment_intent: dict) -> None:
    intent_id = payment_intent["id"]
    await db.execute(
        update(Order)
        .where(Order.stripe_payment_intent_id == intent_id)
        .values(payment_status="failed")
    )


async def _handle_charge_refunded(db: AsyncSession, charge: dict) -> None:
    intent_id = charge.get("payment_intent")
    if intent_id:
        await db.execute(
            update(Order)
            .where(Order.stripe_payment_intent_id == intent_id)
            .values(payment_status="refunded", status="refunded")
        )
