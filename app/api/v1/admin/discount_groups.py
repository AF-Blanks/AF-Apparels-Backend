import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.discount_group import DiscountGroup, VariantPricingOverride, VariantLevelPricingOverride

router = APIRouter(prefix="/admin", tags=["admin", "discount-groups"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ShippingBracket(BaseModel):
    min_units: int = 0
    max_units: int | None = None
    min_order_value: float | None = None
    max_order_value: float | None = None
    cost: float = 0


class DiscountGroupIn(BaseModel):
    title: str
    customer_tag: str | None = None
    applies_to: str = "store"
    applies_to_ids: list[str] = []
    min_req_type: str = "none"
    min_req_value: float | None = None
    shipping_type: str = "store_default"
    shipping_amount: float = 0
    shipping_calc_type: str = "order_value"
    shipping_cutoff_time: str | None = None
    shipping_brackets: list[ShippingBracket] = []
    status: str = "enabled"


class VariantPricingIn(BaseModel):
    overrides: dict[str, dict[str, dict[str, str | None]]]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _group_out(g: DiscountGroup) -> dict:
    try:
        applies_to_ids = json.loads(g.applies_to_ids) if g.applies_to_ids else []
    except Exception:
        applies_to_ids = []
    try:
        shipping_brackets = json.loads(g.shipping_brackets_json) if g.shipping_brackets_json else []
    except Exception:
        shipping_brackets = []
    return {
        "id": str(g.id),
        "title": g.title,
        "customer_tag": g.customer_tag or "",
        "applies_to": g.applies_to,
        "applies_to_ids": applies_to_ids,
        "min_req_type": g.min_req_type,
        "min_req_value": float(g.min_req_value) if g.min_req_value is not None else 0,
        "shipping_type": g.shipping_type,
        "shipping_amount": float(g.shipping_amount) if g.shipping_amount is not None else 0,
        "shipping_calc_type": g.shipping_calc_type or "order_value",
        "shipping_cutoff_time": g.shipping_cutoff_time or "",
        "shipping_brackets": shipping_brackets,
        "status": g.status,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


# ── Discount Group CRUD ───────────────────────────────────────────────────────

@router.get("/discount-groups")
async def list_discount_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DiscountGroup).order_by(DiscountGroup.created_at.desc()))
    groups = result.scalars().all()
    return [_group_out(g) for g in groups]


@router.post("/discount-groups", status_code=status.HTTP_201_CREATED)
async def create_discount_group(body: DiscountGroupIn, db: AsyncSession = Depends(get_db)):
    g = DiscountGroup(
        title=body.title,
        customer_tag=body.customer_tag,
        applies_to=body.applies_to,
        applies_to_ids=json.dumps(body.applies_to_ids) if body.applies_to_ids else None,
        min_req_type=body.min_req_type,
        min_req_value=body.min_req_value,
        shipping_type=body.shipping_type,
        shipping_amount=body.shipping_amount,
        shipping_calc_type=body.shipping_calc_type,
        shipping_cutoff_time=body.shipping_cutoff_time,
        shipping_brackets_json=json.dumps([b.model_dump() for b in body.shipping_brackets]) if body.shipping_brackets else None,
        status=body.status,
    )
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return _group_out(g)


@router.patch("/discount-groups/{group_id}")
async def update_discount_group(
    group_id: UUID, body: DiscountGroupIn, db: AsyncSession = Depends(get_db)
):
    g = await db.get(DiscountGroup, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    data = body.model_dump(exclude_unset=True)
    if "applies_to_ids" in data:
        data["applies_to_ids"] = json.dumps(data["applies_to_ids"]) if data["applies_to_ids"] else None
    if "shipping_brackets" in data:
        brackets = data.pop("shipping_brackets")
        data["shipping_brackets_json"] = json.dumps(brackets) if brackets else None
    for field, val in data.items():
        setattr(g, field, val)
    await db.commit()
    await db.refresh(g)
    return _group_out(g)


@router.delete("/discount-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_discount_group(group_id: UUID, db: AsyncSession = Depends(get_db)):
    g = await db.get(DiscountGroup, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete(g)
    await db.commit()


# ── Variant Pricing Overrides ─────────────────────────────────────────────────

@router.get("/variant-pricing")
async def get_variant_pricing(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VariantPricingOverride))
    rows = result.scalars().all()
    out: dict = {}
    for row in rows:
        pid = row.product_id
        tid = row.tier_id
        if pid not in out:
            out[pid] = {}
        out[pid][tid] = {
            "price": str(row.price) if row.price is not None else "",
            "discount": str(row.discount_percent) if row.discount_percent is not None else "",
        }
    return out


@router.post("/variant-pricing")
async def save_variant_pricing(body: VariantPricingIn, db: AsyncSession = Depends(get_db)):
    for product_id, tier_map in body.overrides.items():
        for tier_id, vals in tier_map.items():
            price_str = (vals.get("price") or "").strip()
            disc_str = (vals.get("discount") or "").strip()
            price = float(price_str) if price_str else None
            discount = float(disc_str) if disc_str else None

            result = await db.execute(
                select(VariantPricingOverride).where(
                    and_(
                        VariantPricingOverride.product_id == product_id,
                        VariantPricingOverride.tier_id == tier_id,
                    )
                )
            )
            existing = result.scalar_one_or_none()

            if price is None and discount is None:
                if existing:
                    await db.delete(existing)
            elif existing:
                existing.price = price
                existing.discount_percent = discount
            else:
                db.add(VariantPricingOverride(
                    product_id=product_id,
                    tier_id=tier_id,
                    price=price,
                    discount_percent=discount,
                ))

    await db.commit()
    return {"ok": True}


# ── Variant-Level Pricing Overrides ───────────────────────────────────────────

class VariantLevelPricingIn(BaseModel):
    overrides: dict  # variantId → groupId → {"price": "12.50"}


@router.get("/variant-level-pricing")
async def get_variant_level_pricing(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(VariantLevelPricingOverride))
    rows = result.scalars().all()
    out: dict = {}
    for row in rows:
        vid = row.variant_id
        gid = row.group_id
        if vid not in out:
            out[vid] = {}
        out[vid][gid] = str(row.price) if row.price is not None else ""
    return out


@router.post("/variant-level-pricing")
async def save_variant_level_pricing(body: VariantLevelPricingIn, db: AsyncSession = Depends(get_db)):
    for variant_id, group_map in body.overrides.items():
        for group_id, price_str in group_map.items():
            price_val = (price_str or "").strip()
            price = float(price_val) if price_val else None

            result = await db.execute(
                select(VariantLevelPricingOverride).where(
                    and_(
                        VariantLevelPricingOverride.variant_id == variant_id,
                        VariantLevelPricingOverride.group_id == group_id,
                    )
                )
            )
            existing = result.scalar_one_or_none()

            if price is None:
                if existing:
                    await db.delete(existing)
            elif existing:
                existing.price = price
            else:
                db.add(VariantLevelPricingOverride(
                    variant_id=variant_id,
                    group_id=group_id,
                    price=price,
                ))

    await db.commit()
    return {"ok": True}
