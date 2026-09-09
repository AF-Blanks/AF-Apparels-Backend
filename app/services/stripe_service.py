"""Taking money through Stripe — cards and US bank debits.

Written to sit beside the QuickBooks Payments path rather than replace it. Which
one runs is decided by PAYMENT_PROVIDER, and until that says "stripe" nothing in
here is reached. That is deliberate: the switch is one setting, flipped when the
account is ready, and flipped back if it is not.

Two things this file is careful about, because both cost real money when they go
wrong.

**Charging twice.** A customer double-clicks Pay, or the network drops the reply
and the browser retries. Every call that moves money carries an idempotency key,
so Stripe itself returns the original charge rather than making a second one —
and the caller passes a key derived from the attempt, not generated here, so a
retry of the *same* attempt reuses it while a genuinely new attempt does not.

**Bank debits are not instant.** A card either works or does not, in a second. A
US bank debit leaves Stripe in `processing` and settles days later, or fails days
later — after the customer has gone. So an ACH payment is never treated as money
received at checkout; it becomes an order that is placed and unpaid, and the
webhook is what marks it paid. Anything that reads a bank debit as immediately
successful is wrong, however convenient.
"""
from __future__ import annotations

import logging
from typing import Any

import stripe

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ── How Stripe's vocabulary maps onto ours ───────────────────────────────────
#
# Stripe has more states than an order needs. These are the only ones that mean
# "the money is here" — everything else is either still moving or has failed,
# and both of those are safer treated as not-paid.
SETTLED = {"succeeded"}

#: Still in flight. A card almost never sits here; a bank debit always does, for
#: days. An order in this state is real and owed, just not yet collected.
IN_FLIGHT = {"processing", "requires_action", "requires_confirmation", "requires_capture"}

#: Nothing was collected and nothing will be without a fresh attempt.
FAILED = {"canceled", "requires_payment_method"}


def payment_status_for(intent_status: str | None) -> str:
    """The order payment_status a Stripe intent status means.

    Anything unrecognised counts as unpaid. A new Stripe status appearing and
    being read as "paid" by default is the one failure mode worth designing out.
    """
    s = (intent_status or "").lower()
    if s in SETTLED:
        return "paid"
    if s in IN_FLIGHT:
        return "pending"
    return "unpaid"


class StripeNotConfigured(RuntimeError):
    """Stripe was asked to do something before it was given keys."""


class StripeService:
    """Everything the app asks Stripe to do.

    Amounts cross this boundary in dollars and are converted to cents here, once
    — the app thinks in dollars everywhere else, and a stray factor of a hundred
    is not a mistake anybody notices in testing.
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._key = (cfg.STRIPE_SECRET_KEY or "").strip()
        self._configured = bool(self._key)
        if self._configured:
            stripe.api_key = self._key
            # Pinned rather than following whatever Stripe ships next: an API
            # version changing under a live checkout is not a thing to discover
            # from a customer.
            stripe.api_version = "2024-06-20"

    # ── guards ──────────────────────────────────────────────────────────────

    def _require_keys(self) -> None:
        if not self._configured:
            raise StripeNotConfigured(
                "Stripe has no secret key configured. Set STRIPE_SECRET_KEY "
                "before switching PAYMENT_PROVIDER to stripe."
            )

    @staticmethod
    def _cents(amount: float) -> int:
        """Dollars to cents, rounded the way money rounds.

        round() alone is banker's rounding — 2.675 goes to 2.67, not 2.68 — so
        a half-cent lands a cent short often enough to matter across a day.
        """
        from decimal import ROUND_HALF_UP, Decimal
        return int(
            (Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

    # ── customers ───────────────────────────────────────────────────────────

    def find_or_create_customer(
        self, *, company_id: str, name: str, email: str | None = None,
    ) -> str:
        """The Stripe customer for one of our companies.

        Looked up by the company id we store on it rather than by email — two
        people at one company share a company, and an email can change.
        """
        self._require_keys()
        found = stripe.Customer.search(
            query=f'metadata["company_id"]:"{company_id}"', limit=1,
        )
        if found.data:
            return found.data[0].id

        created = stripe.Customer.create(
            name=name,
            email=email or None,
            metadata={"company_id": str(company_id)},
            idempotency_key=f"customer:{company_id}",
        )
        logger.info("Stripe customer created for company %s: %s", company_id, created.id)
        return created.id

    # ── taking a card ───────────────────────────────────────────────────────

    def charge_card(
        self,
        *,
        amount: float,
        payment_method_id: str,
        idempotency_key: str,
        customer_id: str | None = None,
        description: str | None = None,
        save_for_future: bool = False,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Charge a card and wait for the answer.

        Confirmed in the same call, so what comes back is the outcome rather
        than a promise of one. A card that needs the cardholder to authenticate
        comes back as requires_action and is treated as not paid — the order is
        refused rather than created against money nobody has.
        """
        self._require_keys()
        params: dict[str, Any] = {
            "amount": self._cents(amount),
            "currency": "usd",
            "payment_method": payment_method_id,
            "confirm": True,
            "description": description or "AF Apparels order",
            "metadata": metadata or {},
            # No redirect-based methods: this is a server-side charge and a
            # bank's authentication page has nowhere to land.
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }
        if customer_id:
            params["customer"] = customer_id
            if save_for_future:
                params["setup_future_usage"] = "off_session"

        intent = stripe.PaymentIntent.create(
            **params, idempotency_key=idempotency_key, expand=["latest_charge"],
        )
        return self._describe(intent)

    def charge_saved_card(
        self,
        *,
        amount: float,
        customer_id: str,
        payment_method_id: str,
        idempotency_key: str,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Charge a card the customer has already given us.

        off_session says the cardholder is not here to authenticate. A card that
        insists on it fails rather than hanging, which is the honest outcome —
        they have to come back and pay attended.
        """
        self._require_keys()
        intent = stripe.PaymentIntent.create(
            amount=self._cents(amount),
            currency="usd",
            customer=customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            description=description or "AF Apparels order",
            metadata=metadata or {},
            idempotency_key=idempotency_key,
            expand=["latest_charge"],
        )
        return self._describe(intent)

    # ── taking a bank debit ─────────────────────────────────────────────────

    def charge_bank_account(
        self,
        *,
        amount: float,
        payment_method_id: str,
        idempotency_key: str,
        customer_id: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        """Debit a US bank account. Returns while it is still in flight.

        This is the whole reason for moving: a bank debit takes days, and what
        comes back here is `processing`, not `succeeded`. The caller must treat
        that as an order placed and unpaid — the webhook, days later, is what
        says whether the money arrived.

        The mandate is what makes the debit lawful: the customer agreed, at a
        given moment, from a given address. Stripe records it when the payment
        method is created on the client, and we keep our own copy too — see
        ach_authorization.py, which the QuickBooks path already uses.
        """
        self._require_keys()
        params: dict[str, Any] = {
            "amount": self._cents(amount),
            "currency": "usd",
            "payment_method": payment_method_id,
            "payment_method_types": ["us_bank_account"],
            "confirm": True,
            "description": description or "AF Apparels order",
            "metadata": metadata or {},
            "mandate_data": {
                "customer_acceptance": {
                    "type": "online",
                    "online": {
                        "ip_address": (metadata or {}).get("ip_address") or "0.0.0.0",
                        "user_agent": (metadata or {}).get("user_agent") or "unknown",
                    },
                }
            },
        }
        if customer_id:
            params["customer"] = customer_id

        intent = stripe.PaymentIntent.create(
            **params, idempotency_key=idempotency_key, expand=["latest_charge"],
        )
        out = self._describe(intent)
        logger.info(
            "Stripe bank debit opened — intent=%s status=%s amount=%.2f",
            out["id"], out["status"], amount,
        )
        return out

    # ── reading one back ────────────────────────────────────────────────────

    def get_payment(self, intent_id: str) -> dict[str, Any]:
        """Where a payment stands now, asked of Stripe rather than remembered."""
        self._require_keys()
        return self._describe(stripe.PaymentIntent.retrieve(intent_id))

    # ── giving it back ──────────────────────────────────────────────────────

    def refund(
        self,
        *,
        intent_id: str,
        idempotency_key: str,
        amount: float | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Refund all or part of a payment.

        Keyed like every other money call, so a refund button pressed twice
        gives the customer their money back once.
        """
        self._require_keys()
        params: dict[str, Any] = {"payment_intent": intent_id}
        if amount is not None:
            params["amount"] = self._cents(amount)
        if reason in ("duplicate", "fraudulent", "requested_by_customer"):
            params["reason"] = reason

        rf = stripe.Refund.create(**params, idempotency_key=idempotency_key)
        logger.info(
            "Stripe refund %s for intent %s — status=%s amount=%s",
            rf.id, intent_id, rf.status, rf.amount,
        )
        return {
            "id": rf.id,
            "status": rf.status,
            "amount": (rf.amount or 0) / 100,
            # A refund can be refused after being accepted, the same trap the
            # QuickBooks eCheck path had: read the status, not the fact that
            # the call returned.
            "succeeded": rf.status in ("succeeded", "pending"),
        }

    # ── saved payment methods ───────────────────────────────────────────────

    def list_saved_cards(self, customer_id: str) -> list[dict[str, Any]]:
        """The cards a customer has left on file, for showing at checkout."""
        self._require_keys()
        methods = stripe.PaymentMethod.list(customer=customer_id, type="card")
        return [
            {
                "id": pm.id,
                "brand": (pm.card or {}).get("brand"),
                "last4": (pm.card or {}).get("last4"),
                "exp_month": (pm.card or {}).get("exp_month"),
                "exp_year": (pm.card or {}).get("exp_year"),
            }
            for pm in methods.data
        ]

    def forget_card(self, payment_method_id: str) -> bool:
        """Detach a saved card. Past charges are unaffected."""
        self._require_keys()
        try:
            stripe.PaymentMethod.detach(payment_method_id)
            return True
        except stripe.StripeError as exc:
            logger.warning("Stripe detach failed for %s: %s", payment_method_id, exc)
            return False

    # ── what the client needs to collect details ────────────────────────────

    def setup_intent(self, *, customer_id: str, idempotency_key: str) -> dict[str, Any]:
        """A SetupIntent, for saving a card or bank account without charging it."""
        self._require_keys()
        si = stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=["card", "us_bank_account"],
            idempotency_key=idempotency_key,
        )
        return {"id": si.id, "client_secret": si.client_secret}

    # ── shared shape ────────────────────────────────────────────────────────

    @staticmethod
    def _describe(intent: Any) -> dict[str, Any]:
        """One shape for every payment, whatever it was paid with.

        The caller should never have to know which corner of a Stripe object a
        card's last four digits live in versus a bank account's.
        """
        # Where the charge lives depends on the API version. Recent ones drop
        # `charges` from the intent entirely and give `latest_charge` instead —
        # an id, or the charge itself when expanded. Reading only the old shape
        # is why charge_id, last4 and brand all came back empty against a real
        # account: the card succeeded and we recorded nothing about it.
        charge = getattr(intent, "latest_charge", None)
        if isinstance(charge, str):
            try:
                charge = stripe.Charge.retrieve(charge)
            except Exception:  # noqa: BLE001 — details are a nicety, not the payment
                charge = None
        if charge is None:
            charges = getattr(intent, "charges", None)
            charge = (charges.data[0] if charges and getattr(charges, "data", None) else None)
        details = (getattr(charge, "payment_method_details", None) or {}) if charge else {}
        card = details.get("card") or {}
        bank = details.get("us_bank_account") or {}

        return {
            "id": intent.id,
            "status": intent.status,
            "payment_status": payment_status_for(intent.status),
            "amount": (intent.amount or 0) / 100,
            "charge_id": getattr(charge, "id", None),
            "customer_id": getattr(intent, "customer", None),
            # What Stripe is still waiting on, if anything. A bank account typed
            # in by hand rather than logged into comes back needing micro-deposit
            # verification: nothing is debited, and without surfacing this the
            # order sits unpaid forever with nobody aware.
            "next_action": ((getattr(intent, "next_action", None) or {}) or {}).get("type"),
            "payment_method_id": getattr(intent, "payment_method", None),
            "method": "bank" if bank else ("card" if card else None),
            "last4": card.get("last4") or bank.get("last4"),
            "brand": card.get("brand") or bank.get("bank_name"),
            # Present only on a failure, and the only thing worth showing a
            # customer — Stripe's own wording, which is written for them.
            "failure_message": (
                (getattr(intent, "last_payment_error", None) or {}).get("message")
                if getattr(intent, "last_payment_error", None) else None
            ),
        }


def is_stripe_active() -> bool:
    """Whether Stripe is the provider taking money right now."""
    cfg = get_settings()
    return (cfg.PAYMENT_PROVIDER or "").strip().lower() == "stripe"
