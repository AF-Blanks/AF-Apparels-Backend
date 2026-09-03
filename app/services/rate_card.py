"""The agreed price list for the top tiers.

Tier 4 and Tier 5 do not buy off the normal price with a discount applied. They
buy off a rate card — a price per product, per size band — agreed with them
directly, and commission is worked out on those figures.

Kept here as data rather than typed into the admin screen variant by variant.
Seventeen products across eight sizes and thirty-odd colours is several thousand
prices; entering that by hand is a morning's work and a typo waiting to happen,
and it has to be redone from scratch every time the card changes.

A blank in the card is not a price of zero and not a free size — it means the
card says nothing about it, and anything the card says nothing about is left
exactly as it is.
"""
from __future__ import annotations

from decimal import Decimal

#: Which card column a catalogue size falls under. Sizes not listed here are not
#: priced by the card and keep whatever they already have.
SIZE_BANDS: dict[str, str] = {
    "XXS": "XS-XL", "XS": "XS-XL", "S": "XS-XL", "M": "XS-XL",
    "L": "XS-XL", "XL": "XS-XL",
    "2XL": "2XL", "XXL": "2XL",
    "3XL": "3XL", "XXXL": "3XL",
    "4XL": "4XL",
    "5XL": "5XL",
}

BANDS = ("XS-XL", "2XL", "3XL", "4XL", "5XL")

#: product code → band → price. None is the card's N/A: no price agreed.
#:
#: The two rows at the foot of the card carry no code — "POLYESTER BASIC
#: T-SHIRT" and "HEAVY WEIGHT T-SHIRT 6.5 OZ" — and are matched to 1010 and 1450
#: by name. Both are named in the preview so a wrong match is seen before it is
#: applied rather than found in an invoice.
RATE_CARD: dict[str, dict[str, Decimal | None]] = {
    # ── Tees ────────────────────────────────────────────────────────────────
    "1000":  {"XS-XL": Decimal("2.20"), "2XL": Decimal("2.40"), "3XL": Decimal("2.75"), "4XL": Decimal("3.00"), "5XL": Decimal("3.50")},
    "1001":  {"XS-XL": Decimal("2.20"), "2XL": Decimal("2.40"), "3XL": Decimal("2.75"), "4XL": Decimal("3.00"), "5XL": Decimal("3.50")},
    "1003":  {"XS-XL": Decimal("2.45"), "2XL": Decimal("2.75"), "3XL": Decimal("3.10"), "4XL": None, "5XL": None},
    "1004":  {"XS-XL": Decimal("3.20"), "2XL": Decimal("3.40"), "3XL": Decimal("3.75"), "4XL": None, "5XL": None},
    "1005":  {"XS-XL": Decimal("2.20"), "2XL": Decimal("2.40"), "3XL": None, "4XL": None, "5XL": None},
    "1133":  {"XS-XL": Decimal("2.15"), "2XL": None, "3XL": None, "4XL": None, "5XL": None},
    "1007":  {"XS-XL": Decimal("1.90"), "2XL": None, "3XL": None, "4XL": None, "5XL": None},
    # ── Fleece ──────────────────────────────────────────────────────────────
    "2011":  {"XS-XL": Decimal("8.50"), "2XL": Decimal("9.00"), "3XL": Decimal("10.00"), "4XL": None, "5XL": None},
    "2013":  {"XS-XL": Decimal("7.25"), "2XL": Decimal("8.00"), "3XL": Decimal("8.50"), "4XL": Decimal("9.25"), "5XL": Decimal("10.00")},
    "1122":  {"XS-XL": Decimal("5.00"), "2XL": None, "3XL": None, "4XL": None, "5XL": None},
    # ── Reserve ─────────────────────────────────────────────────────────────
    "11001": {"XS-XL": Decimal("9.00"), "2XL": Decimal("9.50"), "3XL": Decimal("10.00"), "4XL": None, "5XL": None},
    "11005": {"XS-XL": Decimal("9.00"), "2XL": Decimal("9.50"), "3XL": Decimal("10.00"), "4XL": None, "5XL": None},
    # ── Foot of the card ────────────────────────────────────────────────────
    "5000":  {"XS-XL": Decimal("3.90"), "2XL": Decimal("4.25"), "3XL": Decimal("4.60"), "4XL": Decimal("5.00"), "5XL": Decimal("5.50")},
    "5001":  {"XS-XL": Decimal("4.25"), "2XL": Decimal("4.60"), "3XL": Decimal("4.95"), "4XL": Decimal("5.30"), "5XL": Decimal("5.65")},
    "8001":  {"XS-XL": Decimal("4.50"), "2XL": Decimal("4.95"), "3XL": Decimal("5.44"), "4XL": Decimal("5.81"), "5XL": Decimal("6.58")},
    "1010":  {"XS-XL": Decimal("2.27"), "2XL": Decimal("2.50"), "3XL": Decimal("2.75"), "4XL": Decimal("3.02"), "5XL": Decimal("3.32")},
    "1450":  {"XS-XL": Decimal("3.60"), "2XL": Decimal("3.96"), "3XL": Decimal("4.35"), "4XL": Decimal("4.78"), "5XL": Decimal("5.25")},
}

#: What the card calls the two rows that carry no code, so the preview can say
#: which catalogue product each was matched to.
UNCODED_ROWS: dict[str, str] = {
    "1010": "POLYESTER BASIC T-SHIRT",
    "1450": "HEAVY WEIGHT T-SHIRT 6.5 OZ",
    "8001": "8001 POLYESTER POLO",
}

#: The tiers this card is for. Compared loosely, so "Tier-4" and "tier 4" match.
CARD_TIERS = ("Tier 4", "Tier 5")


def tier_key(name: str | None) -> str:
    """A tier name reduced to what it actually says — see the commission report."""
    return "".join(ch for ch in (name or "").upper() if ch.isalnum())


def product_code_of(product_code: str | None, product_name: str | None) -> str:
    """The catalogue number for a product, from its code or the front of its name."""
    code = (product_code or "").strip()
    if code:
        return code
    lead = (product_name or "").strip().split(" ", 1)[0]
    return lead if lead.isdigit() else ""


def price_for(code: str, size: str | None) -> Decimal | None:
    """What the card says this size of this product costs, if it says anything."""
    row = RATE_CARD.get(code)
    if not row:
        return None
    band = SIZE_BANDS.get((size or "").strip().upper())
    if not band:
        return None
    return row.get(band)
