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
from app.services.quickbooks_service import quickbooks_service
from app.models.inventory import InventoryRecord, Warehouse
from app.models.product import Product, ProductVariant
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
    product = variant.product if variant else None
    return {
        "id": str(li.id),
        "po_id": str(li.po_id),
        "product_variant_id": str(li.product_variant_id) if li.product_variant_id else None,
        "product_name": product.name if product else li.new_product_name,
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
            selectinload(PurchaseOrder.line_items)
            .selectinload(POLineItem.variant)
            .selectinload(ProductVariant.product),
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
        order_date=date.today(),
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


@router.post("/{po_id}/mark-sent")
async def mark_as_sent(po_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id))
    po = result.scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")
    if po.status != "draft":
        raise HTTPException(status_code=400, detail=f"PO is already '{po.status}', cannot mark as sent")
    po.status = "sent"
    await db.commit()
    return {"success": True, "status": "sent"}


# ─── receiving ─────────────────────────────────────────────────────────────────

class ReceivingItemCreate(BaseModel):
    po_line_item_id: str
    qty_received: int
    unit_cost_actual: float


class ReceivingCreate(BaseModel):
    received_date: Optional[date] = None
    notes: Optional[str] = None
    items: list[ReceivingItemCreate]


async def _create_new_product_variant(db: AsyncSession, line_item: "POLineItem") -> "UUID | None":
    """Create a draft Product + ProductVariant for a new-product PO line item. Idempotent by SKU."""
    import re as _re
    product_name = line_item.new_product_name or "Unnamed Product"
    raw_sku = line_item.new_product_sku or ""
    color = line_item.new_product_color or ""
    size = line_item.new_product_size or ""

    # Check existing variant by SKU first
    if raw_sku:
        existing_v = (await db.execute(
            select(ProductVariant).where(ProductVariant.sku == raw_sku)
        )).scalar_one_or_none()
        if existing_v:
            return existing_v.id

    # Find or create the parent product by name
    product = (await db.execute(
        select(Product).where(Product.name == product_name)
    )).scalar_one_or_none()

    if not product:
        base_slug = _re.sub(r"[^a-z0-9]+", "-", product_name.lower()).strip("-") or "product"
        slug, i = base_slug, 1
        while (await db.execute(select(Product).where(Product.slug == slug))).scalar_one_or_none():
            slug = f"{base_slug}-{i}"
            i += 1
        product = Product(name=product_name, slug=slug, status="draft")
        db.add(product)
        await db.flush()

    sku = raw_sku or f"{product.slug}-{color.upper() or 'NA'}-{size.upper() or 'NA'}"
    variant = ProductVariant(
        product_id=product.id,
        sku=sku,
        color=color or None,
        size=size or None,
        retail_price=0.0,
        status="active",
    )
    db.add(variant)
    await db.flush()
    logger.info("Created draft product '%s' variant %s from PO line item", product_name, variant.id)
    return variant.id


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
    variants_received: list[UUID] = []  # collect for QB inventory sync after commit
    for item_data in data.items:
        receiving_item = POReceivingItem(
            receiving_id=receiving.id,
            po_line_item_id=UUID(item_data.po_line_item_id),
            qty_received=item_data.qty_received,
            unit_cost_actual=item_data.unit_cost_actual,
        )
        db.add(receiving_item)
        total_received_this_batch += item_data.qty_received * item_data.unit_cost_actual

        # Update inventory via InventoryRecord
        line_result = await db.execute(
            select(POLineItem).where(POLineItem.id == UUID(item_data.po_line_item_id))
        )
        line_item = line_result.scalar_one_or_none()

        # Auto-create draft product + variant for new-product line items
        if line_item and not line_item.product_variant_id and line_item.new_product_name:
            new_variant_id = await _create_new_product_variant(db, line_item)
            if new_variant_id:
                line_item.product_variant_id = new_variant_id

        if line_item and line_item.product_variant_id:
            try:
                inv_result = await db.execute(
                    select(InventoryRecord).where(
                        InventoryRecord.variant_id == line_item.product_variant_id
                    ).limit(1)
                )
                inv_record = inv_result.scalar_one_or_none()
                if inv_record:
                    inv_record.quantity = inv_record.quantity + item_data.qty_received
                else:
                    wh_result = await db.execute(
                        select(Warehouse).where(Warehouse.is_active == True).limit(1)
                    )
                    warehouse = wh_result.scalar_one_or_none()
                    if warehouse:
                        db.add(InventoryRecord(
                            variant_id=line_item.product_variant_id,
                            warehouse_id=warehouse.id,
                            quantity=item_data.qty_received,
                        ))
                    else:
                        logger.warning("No active warehouse found; skipping inventory update for variant %s", line_item.product_variant_id)

                # Update cost_per_item when actual cost differs from stored cost
                if item_data.unit_cost_actual > 0:
                    variant_obj = (await db.execute(
                        select(ProductVariant).where(ProductVariant.id == line_item.product_variant_id)
                    )).scalar_one_or_none()
                    if variant_obj:
                        old_cost = float(variant_obj.cost_per_item or 0)
                        if abs(item_data.unit_cost_actual - old_cost) > 0.001:
                            variant_obj.cost_per_item = item_data.unit_cost_actual
                            logger.info(
                                "cost_per_item updated: %s $%.2f → $%.2f",
                                variant_obj.sku, old_cost, item_data.unit_cost_actual,
                            )

                if line_item.product_variant_id not in variants_received:
                    variants_received.append(line_item.product_variant_id)

            except Exception as e:
                logger.error("Stock update error for variant %s: %s", line_item.product_variant_id, e, exc_info=True)
                raise

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

    # Dispatch QB vendor bill sync asynchronously (fire-and-forget)
    try:
        from app.tasks.quickbooks_tasks import sync_po_receipt_to_qb
        sync_po_receipt_to_qb.delay(str(po_id), str(receiving.id))
        logger.info("Dispatched sync_po_receipt_to_qb po=%s receiving=%s", po_id, receiving.id)
    except Exception as _e:
        logger.warning("Could not dispatch QB sync task: %s", _e)

    # Dispatch QB inventory+cost sync for each received variant (countdown=5s so cost_per_item commit lands first)
    if variants_received:
        try:
            from app.tasks.quickbooks_tasks import sync_inventory_to_qb
            for vid in variants_received:
                sync_inventory_to_qb.apply_async(args=[str(vid)], countdown=5)
            logger.info("Dispatched sync_inventory_to_qb for %d variants", len(variants_received))
        except Exception as _e:
            logger.warning("Could not dispatch QB inventory sync tasks: %s", _e)

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

    if po.status not in ("draft", "sent"):
        raise HTTPException(
            status_code=400,
            detail="QB PO sync is only available for draft/sent POs. Vendor bill is created automatically on receive.",
        )

    try:
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
        po.qb_synced = True
        await db.commit()
        return {"success": True, "qb_id": qb_result.get("id")}

    except Exception as e:
        logger.error(f"QB sync failed for PO {po_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"QB sync failed: {str(e)}")


# ─── Send email ────────────────────────────────────────────────────────────────

@router.post("/{po_id}/send-email")
async def send_po_email(po_id: UUID, db: AsyncSession = Depends(get_db)):
    import asyncio

    result = await db.execute(
        select(PurchaseOrder)
        .where(PurchaseOrder.id == po_id)
        .options(
            selectinload(PurchaseOrder.manufacturer),
            selectinload(PurchaseOrder.line_items)
            .selectinload(POLineItem.variant)
            .selectinload(ProductVariant.product),
        )
    )
    po = result.scalar_one_or_none()
    if not po:
        raise HTTPException(status_code=404, detail="PO not found")

    manufacturer = po.manufacturer
    if not manufacturer or not manufacturer.email:
        raise HTTPException(status_code=400, detail="Manufacturer has no email address on file")

    # Build line item cards (mobile-friendly)
    rows = ""
    for li in po.line_items:
        variant = li.variant
        product = variant.product if variant else None
        product_name = product.name if product else li.new_product_name or "—"
        color = variant.color if variant else li.new_product_color or "—"
        size = variant.size if variant else li.new_product_size or "—"
        sku = variant.sku if variant else li.new_product_sku or "—"
        total = li.qty_ordered * float(li.unit_cost_expected)

        rows += (
            f'<div style="background:#f9f9f9;border-radius:6px;padding:14px;margin-bottom:10px;border-left:3px solid #1a1a2e;">'
            f'<div style="font-weight:bold;color:#1a1a2e;margin-bottom:8px;font-size:15px;">{product_name}</div>'
            f'<table style="width:100%;border-collapse:collapse;">'
            f'<tr>'
            f'<td style="color:#666;font-size:13px;padding:2px 0;width:50%">SKU: <span style="color:#333;">{sku}</span></td>'
            f'<td style="color:#666;font-size:13px;padding:2px 0;width:50%">Color: <span style="color:#333;">{color}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td style="color:#666;font-size:13px;padding:2px 0;">Size: <span style="color:#333;">{size}</span></td>'
            f'<td style="color:#666;font-size:13px;padding:2px 0;">Qty: <span style="color:#333;">{li.qty_ordered}</span></td>'
            f'</tr>'
            f'<tr>'
            f'<td style="color:#666;font-size:13px;padding:2px 0;">Unit Cost: <span style="color:#333;">${float(li.unit_cost_expected):.2f}</span></td>'
            f'<td style="color:#666;font-size:13px;padding:2px 0;">Total: <span style="font-weight:bold;color:#1a1a2e;">${total:.2f}</span></td>'
            f'</tr>'
            f'</table>'
            f'</div>'
        )

    order_date_str = po.order_date.strftime("%B %d, %Y") if po.order_date else date.today().strftime("%B %d, %Y")
    delivery_str = po.expected_delivery.strftime("%B %d, %Y") if po.expected_delivery else "—"
    contact_str = manufacturer.contact_name or manufacturer.name

    notes_block = (
        f'<p style="margin-top:16px;color:#555;"><strong>Notes:</strong> {po.notes}</p>'
        if po.notes else ""
    )

    import resend as _resend
    from app.core.config import settings as _cfg
    logo_url = _cfg.LOGO_URL or f"{_cfg.FRONTEND_URL}/Af-apparel%20logo.png"
    _resend.api_key = _cfg.RESEND_API_KEY

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<div style="max-width:600px;margin:0 auto;background:#ffffff;">
  <div style="background:#ffffff;padding:20px;text-align:center;border-bottom:2px solid #e63946;">
    <img src="{logo_url}" style="max-height:60px;" alt="AF Apparels"/>
  </div>
  <div style="padding:24px;">
    <h2 style="color:#1a1a2e;margin-top:0;">Purchase Order: {po.po_number}</h2>
    <table style="width:100%;margin-bottom:20px;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;color:#666;width:50%;"><strong>To:</strong> {manufacturer.name}</td>
        <td style="padding:6px 0;color:#666;width:50%;"><strong>Order Date:</strong> {order_date_str}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#666;"><strong>Contact:</strong> {contact_str}</td>
        <td style="padding:6px 0;color:#666;"><strong>Expected Delivery:</strong> {delivery_str}</td>
      </tr>
    </table>
    <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
    <h3 style="color:#1a1a2e;margin-bottom:12px;">Order Items</h3>
    {rows}
    <div style="background:#1a1a2e;color:white;padding:14px 16px;border-radius:6px;margin-top:16px;">
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="font-weight:bold;">Total Expected:</td>
          <td style="font-weight:bold;font-size:18px;text-align:right;">${float(po.total_expected):.2f}</td>
        </tr>
      </table>
    </div>
    {notes_block}
    <p style="margin-top:24px;color:#666;font-size:14px;">
      Please confirm receipt of this purchase order at your earliest convenience.
    </p>
    <hr style="border:none;border-top:1px solid #eee;margin:20px 0;">
    <p style="color:#999;font-size:12px;margin:0;">
      Questions? Call <strong>+1 (469) 367-9753</strong> or
      <a href="mailto:info@afblanks.com" style="color:#e63946;">info@afblanks.com</a><br>
      AF Apparels Wholesale Team | 10719 Turbeville Rd, Dallas, TX 75243
    </p>
  </div>
</div>
</body>
</html>"""

    try:
        response = await asyncio.to_thread(
            _resend.Emails.send,
            {
                "from": f"{_cfg.EMAIL_FROM_NAME} <{_cfg.EMAIL_FROM_ADDRESS}>",
                "to": [manufacturer.email],
                "subject": f"Purchase Order {po.po_number} — AF Apparels",
                "html": html_body,
            }
        )
        logger.info("PO email sent to %s, resend response: %s", manufacturer.email, response)
        ok = True
        if not ok:
            raise HTTPException(status_code=500, detail="Email delivery failed (check RESEND_API_KEY)")

        if po.status == "draft":
            po.status = "sent"
            await db.commit()

        return {"success": True, "message": f"PO email sent to {manufacturer.email}"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("PO email send error for %s: %s", po_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Email failed: {str(e)}")
