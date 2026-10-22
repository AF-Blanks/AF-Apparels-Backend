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


async def stock_on_hand(db: AsyncSession, ids: list[UUID]) -> dict[UUID, int]:
    """What the shelf holds for each variant, for telling a customer how many are
    available. No inventory record reads as 0 here — it says nothing about whether
    a line is a backorder; backorder_flags decides that.
    """
    if not ids:
        return {}
    rows = (await db.execute(
        select(InventoryRecord.variant_id, func.coalesce(func.sum(InventoryRecord.quantity), 0))
        .where(InventoryRecord.variant_id.in_(ids))
        .group_by(InventoryRecord.variant_id)
    )).all()
    found = {vid: int(qty or 0) for vid, qty in rows}
    return {vid: found.get(vid, 0) for vid in ids}


async def backorder_flags(db: AsyncSession, wants: dict[UUID, int]) -> dict[UUID, bool]:
    """Which of these lines are being sold for more than the shelf holds.

    `wants` maps a variant to the quantity being ordered. The rule matches the
    checkout's: a variant nobody keeps stock records for is not a backorder — it
    is untracked, and has always been treated as unlimited — only one that is
    genuinely short and deliberately marked sellable past zero.

    It must keep matching the order writer (order_service.create_order) and the
    guest checkout, which decide the same question for themselves. The wholesale
    checkout asks here before taking the card and the order writer asks after;
    if the two ever answer differently, a card can be charged for an order the
    writer then refuses.

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


# ── The cart: one kind of order at a time ─────────────────────────────────────
#
# The checkout refuses a mixed order (check_not_mixed). By then the customer has
# filled a cart, typed an address and reached for a card — and only then learns
# half of it has to come out. So the cart itself now holds one kind at a time:
# whatever goes in first, in stock or on backorder, decides what may follow,
# until that order is placed or the cart is emptied. Every way into the cart —
# the product page, quick order, a saved template, a reorder, changing a
# quantity — asks here, so none of them can let the other kind slip in.

IN_STOCK = "in_stock"
BACKORDER = "backorder"

IN_STOCK_CART_MESSAGE = (
    "Your cart has in-stock items. Backordered items must be ordered separately — "
    "please complete your current order first, then add these in a new order."
)
BACKORDER_CART_MESSAGE = (
    "Your cart has backordered items. In-stock items must be ordered separately — "
    "please complete your current order first, then add these in a new order."
)
MIXED_SELECTION_MESSAGE = (
    "In-stock and backordered items can't be added together. "
    "Please add them as two separate orders."
)
MIXED_CART_MESSAGE = (
    "Your cart already has both in-stock and backordered items. Please remove one "
    "group from your cart before adding more."
)
BACKORDER_ACK_MESSAGE = (
    "These items are on backorder and will ship when stock arrives. "
    "Please confirm you understand before adding them to your cart."
)


class CartModeConflictError(ConflictError):
    """The cart was asked to hold in-stock and backordered lines together."""

    error_code = "CART_MODE_CONFLICT"
    message = MIXED_SELECTION_MESSAGE


class BackorderAckRequiredError(ConflictError):
    """Backordered lines were sent without the customer confirming they wait."""

    error_code = "BACKORDER_ACK_REQUIRED"
    message = BACKORDER_ACK_MESSAGE


def _mode(flags: dict[UUID, bool]) -> str | None:
    """In stock, backorder, "mixed" for a cart from before this rule, or None if empty."""
    if not flags:
        return None
    waiting = any(flags.values())
    ready = not all(flags.values())
    if waiting and ready:
        return "mixed"
    return BACKORDER if waiting else IN_STOCK


async def _labels(db: AsyncSession, ids: list[UUID]) -> dict[UUID, str]:
    from app.models.product import Product

    rows = (await db.execute(
        select(ProductVariant.id, Product.name, ProductVariant.color, ProductVariant.size, ProductVariant.sku)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(ProductVariant.id.in_(ids))
    )).all()
    return {vid: describe_line(name, color, size, sku) for vid, name, color, size, sku in rows}


async def check_cart_change(
    db: AsyncSession,
    company_id: UUID,
    changes: dict[UUID, int],
    *,
    acknowledged: bool,
    replace: bool = False,
) -> None:
    """Refuse a change that would mix the cart, or add a backorder unconfirmed.

    `changes` maps a variant to a quantity: added to what the cart holds, or —
    with replace=True, for a quantity edited on the cart page — the new total.
    The cart's kind is read from what it holds now; an empty cart takes its kind
    from what is being added, which must then be all one kind itself.

    A backorder line needs `acknowledged`: the customer ticked that it ships when
    stock arrives. Editing the quantity of a line already in a backorder cart was
    confirmed when it went in, so that path passes acknowledged=True.
    """
    from app.models.order import CartItem

    changes = {vid: int(q) for vid, q in changes.items() if int(q or 0) > 0}
    if not changes:
        return

    current: dict[UUID, int] = {}
    for vid, qty in (await db.execute(
        select(CartItem.variant_id, func.sum(CartItem.quantity))
        .where(CartItem.company_id == company_id)
        .group_by(CartItem.variant_id)
    )).all():
        current[vid] = int(qty or 0)

    after = dict(current)
    for vid, qty in changes.items():
        after[vid] = qty if replace else after.get(vid, 0) + qty

    # The cart's kind is what it holds now — the line being edited included, so a
    # cart of one in-stock line still cannot be pushed past the shelf by its own
    # quantity box, nor a lone backorder line edited down into stock.
    mode = _mode(await backorder_flags(db, current))
    after_flags = await backorder_flags(db, after)
    changed = {vid: after_flags.get(vid, False) for vid in changes}

    if mode == "mixed":
        raise CartModeConflictError(message=MIXED_CART_MESSAGE)

    if mode is None:
        if _mode(changed) == "mixed":
            raise CartModeConflictError(message=MIXED_SELECTION_MESSAGE)
    elif mode == IN_STOCK and any(changed.values()):
        if replace:
            stock = await stock_on_hand(db, list(changes))
            names = await _labels(db, list(changes))
            vid = next(v for v, f in changed.items() if f)
            raise CartModeConflictError(
                message=(
                    f"Only {max(stock.get(vid, 0), 0):,} available in stock for {names.get(vid, 'this item')}. "
                    "Your cart is for in-stock items, so the quantity can't go higher."
                ),
            )
        raise CartModeConflictError(message=IN_STOCK_CART_MESSAGE)
    elif mode == BACKORDER and not all(changed.values()):
        if replace:
            stock = await stock_on_hand(db, list(changes))
            names = await _labels(db, list(changes))
            vid = next(v for v, f in changed.items() if not f)
            raise CartModeConflictError(
                message=(
                    f"{max(stock.get(vid, 0), 0):,} of {names.get(vid, 'this item')} are in stock, so this "
                    "quantity can't be in a backorder order. Enter more than that, or remove it and "
                    "order it separately."
                ),
            )
        raise CartModeConflictError(message=BACKORDER_CART_MESSAGE)

    if any(changed.values()) and not acknowledged:
        names = await _labels(db, [v for v, f in changed.items() if f])
        raise BackorderAckRequiredError(details=[{"backordered": sorted(names.values())}])
