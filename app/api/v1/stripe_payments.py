"""What the checkout page needs from Stripe.

The publishable key and a SetupIntent so Stripe's own script can collect card
and bank details in the browser — raw numbers never reach this server on the
Stripe path, which is the point of collecting them this way.

Every route here answers safely while PAYMENT_PROVIDER is still "quickbooks":
`active: false` and nothing else, so the page can ask without needing to know
which provider is switched on.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import ForbiddenError, ValidationError
from app.models.company import Company

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stripe", tags=["payments"])


@router.get("/config")
async def stripe_config() -> dict:
    """Whether Stripe is taking payments, and the key to talk to it with.

    The publishable key is meant to be public — it can only create payment
    methods, never move money. The secret key never leaves the server.
    """
    cfg = get_settings()
    active = (cfg.PAYMENT_PROVIDER or "").strip().lower() == "stripe"
    return {
        "active": active,
        "publishable_key": cfg.STRIPE_PUBLISHABLE_KEY if active else None,
        # What the page should offer. Bank debits are the reason for moving, so
        # they are on wherever Stripe is.
        "methods": ["card", "us_bank_account"] if active else [],
    }


async def _company_customer(db: AsyncSession, request: Request) -> tuple[Company, str]:
    """This company and its Stripe customer, creating the customer if needed."""
    from app.services.stripe_service import StripeService

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    if not company:
        raise ValidationError("Company not found")

    if company.stripe_customer_id:
        return company, company.stripe_customer_id

    svc = StripeService()
    cust_id = await asyncio.to_thread(
        lambda: svc.find_or_create_customer(
            company_id=str(company.id),
            name=company.name or f"Company {company.id}",
            # The column is company_email; there is no `email` on Company, and
            # reading one raised straight past the error handling as a 500 with
            # no CORS headers — which reaches the browser as "Failed to fetch"
            # and tells nobody anything.
            email=company.company_email,
        )
    )
    company.stripe_customer_id = cust_id
    await db.commit()
    return company, cust_id


@router.post("/setup-intent")
async def create_setup_intent(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """A client secret for collecting a card or bank account without charging it.

    Used to save a payment method for next time, and to collect bank details for
    a debit — Stripe's script needs one of these to work against.
    """
    from app.services.stripe_service import StripeService, is_stripe_active

    if not is_stripe_active():
        raise ValidationError("Stripe is not the active payment provider.")

    company, cust_id = await _company_customer(db, request)
    svc = StripeService()
    out = await asyncio.to_thread(
        lambda: svc.setup_intent(
            customer_id=cust_id,
            # New every time: a SetupIntent is cheap and reusing one across two
            # different cards would attach the second to the first's record.
            idempotency_key=f"setup:{company.id}:{uuid.uuid4()}",
        )
    )
    return {"client_secret": out["client_secret"]}


@router.get("/saved-cards")
async def list_saved_cards(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Cards this company has left on file.

    Answers with an empty list rather than an error when Stripe is off, so the
    checkout page can ask unconditionally.
    """
    from app.services.stripe_service import StripeService, is_stripe_active

    if not is_stripe_active():
        return {"cards": []}

    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    company = (await db.execute(
        select(Company).where(Company.id == company_id)
    )).scalar_one_or_none()
    if not company or not company.stripe_customer_id:
        return {"cards": []}

    svc = StripeService()
    try:
        cards = await asyncio.to_thread(lambda: svc.list_saved_cards(company.stripe_customer_id))
    except Exception as exc:  # noqa: BLE001 — a saved-card list is a convenience
        logger.warning("Could not list Stripe cards for company %s: %s", company_id, exc)
        return {"cards": []}
    return {"cards": cards}


@router.delete("/saved-cards/{payment_method_id}")
async def forget_saved_card(
    payment_method_id: str, request: Request, db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a saved card. Past charges and refunds are unaffected."""
    from app.services.stripe_service import StripeService, is_stripe_active

    if not is_stripe_active():
        raise ValidationError("Stripe is not the active payment provider.")

    company, cust_id = await _company_customer(db, request)

    # A card id is guessable; without this check anyone signed in could detach
    # somebody else's card by naming it.
    svc = StripeService()
    mine = await asyncio.to_thread(lambda: svc.list_saved_cards(cust_id))
    if payment_method_id not in {c["id"] for c in mine}:
        raise ForbiddenError("That card is not on this account.")

    ok = await asyncio.to_thread(lambda: svc.forget_card(payment_method_id))
    return {"removed": ok}
