"""One order ships once, so everything on it has to be shippable together.

A cart holding a white tee that is on the shelf and a black one that is not
cannot be filled honestly. Either the in-stock half sits waiting on goods that
may be weeks out, or the order is quietly split into two deliveries the
customer was billed once for. Neither is a call this system should make on a
customer's behalf, so a mixed order is refused and they are asked to place the
two halves separately — the in-stock one then ships today.

Every door into an order asks the same two questions here — is this line short,
and do these lines clash — so wholesale checkout, guest checkout and an order an
admin builds by hand cannot drift apart on the answer.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.models.inventory import InventoryRecord
from app.models.product import ProductVariant


class MixedOrderError(ConflictError):
    """An order was asked to carry both in-stock and backordered lines."""

    error_code = "MIXED_BACKORDER"
    message = (
        "This order has both in-stock and backordered items. "
        "Please place them as two separate orders."
    )


async def backorder_flags(db: AsyncSession, wants: dict[UUID, int]) -> dict[UUID, bool]:
    """Which of these lines are being sold for more than the shelf holds.

    `wants` maps a variant to the quantity being ordered. The rule matches the
    checkout's: a variant nobody keeps stock records for is not a backorder — it
    is untracked, and has always been treated as unlimited — only one that is
    genuinely short and deliberately marked sellable past zero.

    Answered for the whole basket in two queries rather than two per line, since
    every caller here is looking at a cart, not a single item.
    """
    if not wants:
        return {}
    ids = list(wants)

    stock: dict[UUID, tuple[int, int]] = {
        vid: (int(qty or 0), int(records or 0))
        for vid, qty, records in (await db.execute(
            select(
                InventoryRecord.variant_id,
                func.coalesce(func.sum(InventoryRecord.quantity), 0),
                func.count(InventoryRecord.id),
            )
            .where(InventoryRecord.variant_id.in_(ids))
            .group_by(InventoryRecord.variant_id)
        )).all()
    }

    allowed: dict[UUID, bool] = {
        vid: bool(flag)
        for vid, flag in (await db.execute(
            select(ProductVariant.id, ProductVariant.allow_backorder)
            .where(ProductVariant.id.in_(ids))
        )).all()
    }

    out: dict[UUID, bool] = {}
    for vid, qty in wants.items():
        on_hand, records = stock.get(vid, (0, 0))
        out[vid] = allowed.get(vid, False) and records > 0 and on_hand < int(qty or 0)
    return out


def describe_line(product_name: str | None, color: str | None, size: str | None, sku: str | None) -> str:
    """How a line is named back to whoever has to act on it.

    A SKU alone is no help to a customer deciding what to take out of the cart,
    and a product name alone doesn't say which colour is the problem.
    """
    variant = " / ".join(p for p in (color, size) if p)
    label = product_name or sku or "Item"
    if variant:
        label = f"{label} — {variant}"
    return label


def check_not_mixed(lines: list[dict]) -> None:
    """Refuse an order that is part in stock and part on backorder.

    Each line is a dict with "label" and "backordered". An order that is wholly
    one or the other is fine: all in stock ships now, all backordered waits
    together for one delivery. It is only the mixture that has no good outcome.
    """
    ready = [l["label"] for l in lines if not l.get("backordered")]
    waiting = [l["label"] for l in lines if l.get("backordered")]
    if not ready or not waiting:
        return

    def _listed(names: list[str], limit: int = 4) -> str:
        shown = names[:limit]
        rest = len(names) - len(shown)
        text = ", ".join(shown)
        return f"{text} and {rest} more" if rest > 0 else text

    raise MixedOrderError(
        message=(
            "This order has both in-stock and backordered items, which can't ship "
            "together. Please place them as two separate orders so the in-stock "
            f"items go out straight away. In stock now: {_listed(ready)}. "
            f"On backorder: {_listed(waiting)}."
        ),
        details=[
            {"group": "in_stock", "items": ready},
            {"group": "backordered", "items": waiting},
        ],
    )
