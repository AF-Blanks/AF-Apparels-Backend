"""Proof that a customer allowed us to take money out of their bank account.

A bank debit is not like a card. The customer's bank can be told, up to two
years later, that the debit was never authorised — and then the money goes
back and we are asked to show otherwise. Checking a box at checkout is the
authorisation; keeping a record of it is what makes the authorisation worth
anything afterwards.

So what is kept is not a "yes" flag but the three things a dispute asks for:
what the customer was shown, when they agreed, and from where.
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy import text as _sql
from sqlalchemy.ext.asyncio import AsyncSession

#: What the checkout shows above the box. Held here as well as on the page so a
#: record still means something if the page ever sends nothing, and so the
#: wording can be read without digging through the frontend.
AUTHORIZATION_TEXT = (
    "I authorise AF Apparels to debit the bank account above for the total of "
    "this order. This authorisation is for this order only. If the transfer is "
    "returned unpaid, I understand AF Apparels may charge a returned-item fee. "
    "To withdraw this authorisation, contact AF Apparels before the transfer is "
    "processed."
)


def client_ip(request: Request | None) -> str | None:
    """The customer's address, not our proxy's.

    Everything reaches this app through Railway's edge, so request.client.host
    is the proxy on every single request and would make every authorisation
    look like it came from the same place. The first hop in X-Forwarded-For is
    the caller; the rest are proxies that added themselves on the way.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first[:64]
    return (request.client.host[:64] if request.client else None)


async def record_authorization(
    db: AsyncSession,
    order_id,
    request: Request | None,
    shown_text: str | None = None,
) -> None:
    """Write down that this order's debit was authorised, and on what terms.

    Written with raw SQL, and never allowed to raise: an order must not fail
    because we could not file the paperwork for it. A missing record is a
    problem for us later, not for the customer now.
    """
    try:
        await db.execute(
            _sql(
                "UPDATE orders SET ach_authorized_at = now(),"
                " ach_authorized_ip = :ip, ach_authorization_text = :txt"
                " WHERE id = :oid"
            ),
            {
                "ip": client_ip(request),
                "txt": (shown_text or AUTHORIZATION_TEXT)[:2000],
                "oid": str(order_id),
            },
        )
    except Exception:  # pragma: no cover — best effort by design
        import logging

        logging.getLogger(__name__).warning(
            "Could not record ACH authorisation for order %s", order_id, exc_info=True
        )
