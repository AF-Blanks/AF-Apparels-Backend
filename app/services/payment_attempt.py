"""One press of Pay, however many times it arrives.

A customer double-clicks. A phone loses signal after the request goes out but
before the answer comes back, and the browser retries. A tab is left open and
refreshed. Every one of these sends the same checkout twice, and without
something in the way, the second one takes their money again.

Three layers stop it, and all three are needed:

1. The browser sends an attempt id — one value, made once when the checkout form
   is opened, reused on every retry of that same attempt. A genuinely new
   attempt (they went back, changed the cart, tried again) makes a new one.

2. This table claims that id before anything is charged. The claim is a unique
   insert, so of two requests arriving at the same instant, exactly one wins —
   the database decides, not the order the two happen to run in. The loser waits
   and returns whatever the winner produced.

3. The same id is passed to Stripe as its idempotency key, so even if both
   layers above were somehow bypassed, Stripe returns the first charge instead
   of making a second.

The first two also cover the case Stripe cannot: a request that fails *after*
the charge succeeds — the money is taken and the order is not written. The
attempt row remembers the charge, so the retry finishes the order instead of
paying for it again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: How long a claimed-but-unfinished attempt blocks a retry. Long enough that a
#: slow card (Stripe allows up to a minute or so) is never overtaken by the
#: customer's second click; short enough that a genuinely stuck attempt does not
#: lock someone out of buying for the rest of the day.
STALE_AFTER = timedelta(minutes=5)


class AttemptInFlight(Exception):
    """This exact attempt is already being charged somewhere else, right now."""


class AttemptAlreadyDone(Exception):
    """This attempt already produced an order. Here it is again.

    Carries the order id so the caller can return the original rather than
    reporting an error for what was, from the customer's side, a success.
    """

    def __init__(self, order_id: str, payment_reference: str | None = None):
        super().__init__(f"Attempt already completed as order {order_id}")
        self.order_id = order_id
        self.payment_reference = payment_reference


async def claim(db: AsyncSession, *, attempt_key: str, company_id: str | None) -> None:
    """Take this attempt, or say who already has it.

    Raises AttemptAlreadyDone if it finished, AttemptInFlight if it is running.
    Returning normally means the caller owns it and may charge.
    """
    row = (await db.execute(text(
        "SELECT status, order_id, payment_reference, created_at "
        "FROM payment_attempts WHERE attempt_key = :k"
    ), {"k": attempt_key})).mappings().first()

    if row:
        if row["status"] == "completed" and row["order_id"]:
            raise AttemptAlreadyDone(str(row["order_id"]), row["payment_reference"])
        if row["status"] == "in_flight":
            started = row["created_at"]
            if started and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started and datetime.now(timezone.utc) - started < STALE_AFTER:
                raise AttemptInFlight()
            # Older than that and nothing came of it — the worker died, or the
            # charge never returned. Let this request take it over rather than
            # leaving the customer unable to buy.
            logger.warning(
                "payment attempt %s was in flight since %s — stale, retaking",
                attempt_key, started,
            )
            await db.execute(text(
                "UPDATE payment_attempts SET status = 'in_flight', created_at = now() "
                "WHERE attempt_key = :k"
            ), {"k": attempt_key})
            await db.commit()
            return
        # 'failed' — a previous try was declined. Trying again is the point.
        await db.execute(text(
            "UPDATE payment_attempts SET status = 'in_flight', created_at = now(), "
            "failure_reason = NULL WHERE attempt_key = :k"
        ), {"k": attempt_key})
        await db.commit()
        return

    # Nothing yet. The unique index on attempt_key is what makes this a race
    # nobody can both win: a second request arriving in the same instant hits
    # the conflict, finds no row of its own, and is told to wait.
    inserted = (await db.execute(text(
        "INSERT INTO payment_attempts (attempt_key, company_id, status) "
        "VALUES (:k, CAST(NULLIF(:c, '') AS UUID), 'in_flight') "
        "ON CONFLICT (attempt_key) DO NOTHING "
        "RETURNING id"
    ), {"k": attempt_key, "c": company_id or ""})).first()
    await db.commit()

    if inserted is None:
        raise AttemptInFlight()


async def completed(
    db: AsyncSession, *, attempt_key: str, order_id: str,
    payment_reference: str | None = None,
) -> None:
    """This attempt produced an order. A retry now gets that order back."""
    await db.execute(text(
        "UPDATE payment_attempts SET status = 'completed', order_id = CAST(:o AS UUID), "
        "payment_reference = :p, completed_at = now() WHERE attempt_key = :k"
    ), {"k": attempt_key, "o": order_id, "p": payment_reference})
    await db.commit()


async def failed(db: AsyncSession, *, attempt_key: str, reason: str) -> None:
    """Nothing was collected. The customer may try again with the same key."""
    await db.execute(text(
        "UPDATE payment_attempts SET status = 'failed', failure_reason = :r "
        "WHERE attempt_key = :k"
    ), {"k": attempt_key, "r": (reason or "")[:500]})
    await db.commit()


async def release(db: AsyncSession, *, attempt_key: str) -> None:
    """Give the attempt back unused — nothing was charged, nothing was created.

    For the checks that run before any money moves: a mixed order, an empty
    cart, a bad address. Those should not consume the attempt, or correcting the
    problem and pressing Pay again would be refused as a duplicate.
    """
    await db.execute(text(
        "DELETE FROM payment_attempts WHERE attempt_key = :k AND status = 'in_flight'"
    ), {"k": attempt_key})
    await db.commit()
