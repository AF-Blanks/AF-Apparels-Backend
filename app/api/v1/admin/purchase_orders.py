"""Admin — Purchase Order management (manufacturers, POs, receiving, QB sync)."""
import logging
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.models.product import ProductVariant
from app.models.purchase_order import (
    Manufacturer,
    POLineItem,
    POReceiving,
    POReceivingItem,
    PurchaseOrder,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── helpers ──────────────────────────────────────────────────────────────────

def _mfr_dict(m: Manufacturer) -> dict:
    return {
        "id": str(m.id),
        "name": m.name,
        "contact_name": m.contact_name,
        "email": m.email,
        "phone": m.phone,
        "address": m.address,
        "notes": m.notes,
        "is_active": m.is_active,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _line_item_dict(li: POLineItem) -> dict:
    variant = li.variant
    return {
        "id": str(li.id),
        "po_id": str(li.po_id),
        "product_variant_id": str(li.product_variant_id) if li.product_variant_id else None,
        "variant_sku": variant.sku if variant else None,
        "variant_color": variant.color if variant else li.new_product_color,
        "variant_size": variant.size if variant else li.new_product_size,
        "new_product_name": li.new_product_name,
        "new_product_sku": li.new_product_sku,
        "new_product_size": li.new_product_size,
        "new_product_color": li.new_product_color,
        "qty_ordered": li.qty_ordered,
        "unit_cost_expected": float(li.unit_cost_expected),
        "total_expected": float(li.total_expected) if li.total_expected else 0.0,
    }


def _receiving_dict(r: POReceiving) -> dict:
    return {
        "id": str(r.id),
        "po_id": str(r.po_id),
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "notes": r.notes,
        "qb_bill_id": r.qb_bill_id,
        "qb_synced": r.qb_synced,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "items": [
            {
                "id": str(ri.id),
                "po_line_item_id": str(ri.po_line_item_id) if ri.po_line_item_id else None,
                "qty_received": ri.qty_received,
                "unit_cost_actual": float(ri.unit_cost_actual),
                "total_actual": float(ri.total_actual) if ri.total_actual else 0.0,
            }
            for ri in r.items
        ],
    }


def _po_dict(po: PurchaseOrder, include_detail: bool = False) -> dict:
    d: dict = {
        "id": str(po.id),
        "po_number": po.po_number,
        "manufacturer_id": str(po.manufacturer_id) if po.manufacturer_id else None,
        "manufacturer_name": po.manufacturer.name if po.manufacturer else None,
        "status": po.status,
        "order_date": po.order_date.isoformat() if po.order_date else None,
        "expected_delivery": po.expected_delivery.isoformat() if po.expected_delivery else None,
        "notes": po.notes,
        "total_expected": float(po.total_expected),
        "total_received": float(po.total_received),
        "qb_synced": po.qb_synced,
        "qb_po_id": po.qb_po_id,
        "qb_bill_id": po.qb_bill_id,
        "created_at": po.created_at.isoformat() if po.created_at else None,
        "item_count": len(po.line_items),
    }
    if include_detail:
        d["line_items"] = [_line_item_dict(li) for li in po.line_items]
        d["receivings"] = [_receiving_dict(r) for r in po.receivings]
    return d


# ─── manufacturers ─────────────────────────────────────────────────────────────

@router.get("/manufacturers")
async def list_manufacturers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Manufacturer).where(Manufacturer.is_active == True).order_by(Manufacturer.name)
    )
    return [_mfr_dict(m) for m in result.scalars().all()]


class ManufacturerCreate(BaseModel):
    name: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None


@router.post("/manufacturers")
async def create_manufacturer(data: ManufacturerCreate, db: AsyncSession = Depends(get_db)):
    manufacturer = Manufacturer(**data.model_dump())
    db.add(manufacturer)
    await db.commit()
    await db.refresh(manufacturer)
    return _mfr_dict(manufacturer)


@router.put("/manufacturers/{mfr_id}")
async def update_manufacturer(
    mfr_id: UUID, data: ManufacturerCreate, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Manufacturer).where(Manufacturer.id == mfr_id))
    manufacturer = result.scalar_one_or_none()
    if not manufacturer:
        raise HTTPException(status_code=404, detail="Manufacturer not found")
    for key, value in data.model_dump().items():
        setattr(manufacturer, key, value)
    await db.commit()
    await db.refresh(manufacturer)
    return _mfr_dict(manufacturer)


@router.delete("/manufacturers/{mfr_id}")
async def delete_manufacturer(mfr_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Manufacturer).where(Manufacturer.id == mfr_id))
    manufacturer = result.scalar_one_or_none()
    if not manufacturer:
        raise HTTPException(status_code=404, detail="Manufacturer not found")
    manufacturer.is_active = False
    await db.commit()
    return {"success": True}


# ─── purchase orders ───────────────────────────────────────────────────────────

@router.get("/")
async def list_pos(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PurchaseOrder)
        .options(
            selectinload(PurchaseOrder.manufacturer),
            selectinload(PurchaseOrder.line_items),
        )
        .order_by(PurchaseOrder.created_at.desc())
    )
    return [_po_dict(po) for po in result.scalars().all()]


@router.get("/{po_id}")
async def get_po(po_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == po_id)
        .options(
            selectinload(PurchaseOrder.manufacturer),
            selectinload(PurchaseOrder.line_items).selectinload(POLineItem.variant),
            selectinload(PurchaseOrder.receivings).selectinload(POReceiving.items),
        )
    )
    po = result.scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    return _po_dict(po, include_detail=True)


class POLineItemCreate(BaseModel):
    product_variant_id: Optional[str] = None
    new_product_name: Optional[str] = None
    new_product_sku: Optional[str] = None
    new_product_size: Optional[str] = None
    new_product_color: Optional[str] = None
    qty_ordered: int
    unit_cost_expected: float


class POCreate(BaseModel):
    manufacturer_id: str
    expected_delivery: Optional[date] = None
    notes: Optional[str] = None
    line_items: list[POLineItemCreate]


@router.post("/")
async def create_po(data: POCreate, db: AsyncSession = Depends(get_db)):
    po = PurchaseOrder(
        manufacturer_id=UUID(data.manufacturer_id),
        expected_delivery=data.expected_delivery,
        notes=data.notes,
        status="draft",
    )
    db.add(po)
    await db.flush()  # trigger assigns po_number

    total_expected = 0.0
    for item_data in data.line_items:
        line_item = POLineItem(
            po_id=po.id,
            product_variant_id=UUID(item_data.product_variant_id) if item_data.product_variant_id else None,
            new_product_name=item_data.new_product_name,
            new_product_sku=item_data.new_product_sku,
            new_product_size=item_data.new_product_size,
            new_product_color=item_data.new_product_color,
            qty_ordered=item_data.qty_ordered,
            unit_cost_expected=item_data.unit_cost_expected,
        )
        db.add(line_item)
        total_expected += item_data.qty_ordered * item_data.unit_cost_expected

    po.total_expected = total_expected
    await db.commit()
    await db.refresh(po)
    return {"id": str(po.id), "po_number": po.po_number}


@router.patch("/{po_id}/status")
async def update_po_status(po_id: UUID, body: dict, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id))
    po = result.scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    new_status = body.get("status")
    if new_status not in ("draft", "sent", "partial", "received", "closed", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    po.status = new_status
    await db.commit()
    return {"success": True, "status": new_status}


# ─── receiving ─────────────────────────────────────────────────────────────────

class ReceivingItemCreate(BaseModel):
    po_line_item_id: str
    qty_received: int
    unit_cost_actual: float


class ReceivingCreate(BaseModel):
    received_date: Optional[date] = None
    notes: Optional[str] = None
    items: list[ReceivingItemCreate]


@router.post("/{po_id}/receive")
async def receive_items(po_id: UUID, data: ReceivingCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == po_id)
        .options(selectinload(PurchaseOrder.line_items))
    )
    po = result.scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    receiving = POReceiving(
        po_id=po_id,
        received_date=data.received_date or date.today(),
        notes=data.notes,
    )
    db.add(receiving)
    await db.flush()

    total_received_this_batch = 0.0
    for item_data in data.items:
        receiving_item = POReceivingItem(
            receiving_id=receiving.id,
            po_line_item_id=UUID(item_data.po_line_item_id),
            qty_received=item_data.qty_received,
            unit_cost_actual=item_data.unit_cost_actual,
        )
        db.add(receiving_item)
        total_received_this_batch += item_data.qty_received * item_data.unit_cost_actual

        # Update inventory
        line_result = await db.execute(
            select(POLineItem).where(POLineItem.id == UUID(item_data.po_line_item_id))
        )
        line_item = line_result.scalar_one_or_none()
        if line_item and line_item.product_variant_id:
            variant_result = await db.execute(
                select(ProductVariant).where(ProductVariant.id == line_item.product_variant_id)
            )
            variant = variant_result.scalar_one_or_none()
            if variant:
                variant.stock_quantity = (variant.stock_quantity or 0) + item_data.qty_received

    po.total_received = float(po.total_received or 0) + total_received_this_batch

    # Recalculate status
    total_qty_ordered = sum(li.qty_ordered for li in po.line_items)
    qty_recv_result = await db.execute(
        select(func.sum(POReceivingItem.qty_received))
        .join(POReceiving)
        .where(POReceiving.po_id == po_id)
    )
    total_qty_received = qty_recv_result.scalar() or 0

    if total_qty_received >= total_qty_ordered:
        po.status = "received"
    else:
        po.status = "partial"

    await db.commit()
    return {"success": True, "receiving_id": str(receiving.id)}


# ─── QB sync ──────────────────────────────────────────────────────────────────

@router.post("/{po_id}/sync-qb")
async def sync_to_quickbooks(po_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == po_id)
        .options(
            selectinload(PurchaseOrder.manufacturer),
            selectinload(PurchaseOrder.line_items),
            selectinload(PurchaseOrder.receivings).selectinload(POReceiving.items)
            .selectinload(POReceivingItem.line_item),
        )
    )
    po = result.scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    manufacturer = po.manufacturer
    if not manufacturer:
        raise HTTPException(status_code=400, detail="PO has no manufacturer")

    from app.services.quickbooks_service import quickbooks_service

    try:
        if po.status in ("draft", "sent"):
            qb_result = await quickbooks_service.create_purchase_order(
                vendor_name=manufacturer.name,
                line_items=[
                    {
                        "description": li.new_product_name or f"SKU {li.new_product_sku or li.product_variant_id}",
                        "qty": li.qty_ordered,
                        "unit_price": float(li.unit_cost_expected),
                    }
                    for li in po.line_items
                ],
                po_number=po.po_number,
                expected_date=str(po.expected_delivery) if po.expected_delivery else None,
            )
            po.qb_po_id = qb_result.get("id")
        else:
            all_items = []
            for receiving in po.receivings:
                for ri in receiving.items:
                    li = ri.line_item
                    all_items.append({
                        "description": li.new_product_name if li else "Item",
                        "qty": ri.qty_received,
                        "unit_price": float(ri.unit_cost_actual),
                    })
            qb_result = await quickbooks_service.create_vendor_bill(
                vendor_name=manufacturer.name,
                line_items=all_items,
                po_number=po.po_number,
                bill_date=str(date.today()),
            )
            po.qb_bill_id = qb_result.get("id")

        po.qb_synced = True
        await db.commit()
        return {"success": True, "qb_id": qb_result.get("id")}

    except Exception as e:
        logger.error(f"QB sync error for PO {po_id}: {e}")
        raise HTTPException(status_code=500, detail=f"QB sync failed: {str(e)}")
