"""Box-packing calculator for multi-box Shippo label generation."""
import math
from dataclasses import dataclass

FALLBACK_WEIGHT_G = 180.0  # 1 standard T-shirt unit in grams
GRAMS_PER_LB = 453.592
UNITS_PER_BOX = 72         # max T-shirt-equivalent units per box
BOX_LENGTH = "20"
BOX_WIDTH = "16"
BOX_HEIGHT = "12"          # inches (standard apparel box)


@dataclass
class BoxSpec:
    box_number: int
    weight_lbs: float


def _item_multiplier(product_name: str, size: str | None) -> float:
    """Return the T-shirt-equivalent weight multiplier for one unit of this item."""
    name_l = (product_name or "").lower()
    sz = (size or "").upper().replace(" ", "")

    if any(k in name_l for k in ("hoodie", "sweatshirt", "crop hoodie")):
        return 7.5
    if "long sleeve" in name_l or "longsleeve" in name_l:
        return 1.5
    # T-shirt — size-based override
    if sz in ("4XL", "5XL"):
        return 1.5
    if sz == "3XL":
        return 1.2
    return 1.0  # S, M, L, XL, 2XL T-shirts


def calculate_boxes(items, variant_weight_g: dict | None = None, override_count: int | None = None) -> list[BoxSpec]:
    """Calculate how many boxes an order needs and the weight per box.

    Args:
        items: list of OrderItem objects with .product_name, .size, .quantity, .variant_id
        variant_weight_g: optional dict of str(variant_id) -> grams
        override_count: if set (>=1), forces this many boxes and splits the total
            weight evenly across them — an admin manually adjusting how the order
            was actually packed (fewer/more boxes than the auto estimate).

    Returns:
        List of BoxSpec objects, minimum 1 box.
    """
    if not items:
        n = int(override_count) if override_count and override_count >= 1 else 1
        return [BoxSpec(box_number=i + 1, weight_lbs=1.0) for i in range(n)]

    total_units = 0.0
    total_weight_g = 0.0

    for item in items:
        mult = _item_multiplier(item.product_name, item.size)
        qty = item.quantity or 1
        total_units += qty * mult

        vid = str(item.variant_id) if item.variant_id else None
        if variant_weight_g and vid and vid in variant_weight_g:
            total_weight_g += variant_weight_g[vid] * qty
        else:
            # Fallback: 180g per T-shirt-equivalent unit
            total_weight_g += FALLBACK_WEIGHT_G * qty * mult

    num_boxes = max(1, math.ceil(total_units / UNITS_PER_BOX))
    if override_count and override_count >= 1:
        num_boxes = int(override_count)  # admin manually set how many boxes were used
    total_lbs = total_weight_g / GRAMS_PER_LB
    per_box_lbs = total_lbs / num_boxes

    return [
        BoxSpec(box_number=i + 1, weight_lbs=round(per_box_lbs, 3))
        for i in range(num_boxes)
    ]
