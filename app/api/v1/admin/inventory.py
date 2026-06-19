from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.inventory import (
    AdjustmentResult,
    BulkImportResult,
    InventoryAdjustRequest,
    WarehouseCreate,
    WarehouseOut,
)
from app.services.inventory_service import InventoryService

router = APIRouter(prefix="/admin", tags=["admin", "inventory"])


@router.get("/warehouses", response_model=list[WarehouseOut])
async def list_warehouses(db: AsyncSession = Depends(get_db)):
    svc = InventoryService(db)
    return await svc.list_warehouses()


@router.post("/warehouses", response_model=WarehouseOut, status_code=status.HTTP_201_CREATED)
async def create_warehouse(payload: WarehouseCreate, db: AsyncSession = Depends(get_db)):
    svc = InventoryService(db)
    wh = await svc.create_warehouse(
        payload.name,
        payload.code,
        address_line1=payload.address_line1,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        country=payload.country,
    )
    await db.commit()
    return wh


@router.patch("/warehouses/{warehouse_id}", response_model=WarehouseOut)
async def update_warehouse(
    warehouse_id: UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    svc = InventoryService(db)
    wh = await svc.update_warehouse(warehouse_id, payload)
    await db.commit()
    return wh


@router.get("/inventory")
async def list_inventory(
    variant_id: UUID | None = None,
    warehouse_id: UUID | None = None,
    low_stock_only: bool = False,
    db: AsyncSession = Depends(get_db),
):
    svc = InventoryService(db)
    if variant_id:
        return await svc.get_inventory_by_variant(variant_id)
    if low_stock_only:
        return await svc.get_low_stock_variants()
    # Return all — left join so variants without inventory records also appear
    from sqlalchemy import select
    from app.models.inventory import InventoryRecord, Warehouse
    from app.models.product import Product, ProductVariant

    result = await db.execute(
        select(ProductVariant, InventoryRecord, Warehouse, Product.name.label("product_name"))
        .join(Product, ProductVariant.product_id == Product.id)
        .outerjoin(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
        .outerjoin(Warehouse, InventoryRecord.warehouse_id == Warehouse.id)
        .where(ProductVariant.status != "discontinued")
        .order_by(Product.name, ProductVariant.color, ProductVariant.size)
        .limit(500)
    )
    return [
        {
            "variant_id": str(v.id),
            "sku": v.sku,
            "color": v.color,
            "size": v.size,
            "product_name": product_name,
            "warehouse_id": str(wh.id) if wh else None,
            "warehouse_name": wh.name if wh else "—",
            "quantity": rec.quantity if rec else 0,
            "low_stock_threshold": rec.low_stock_threshold if rec else 10,
        }
        for v, rec, wh, product_name in result.all()
    ]


@router.post("/inventory/adjust", response_model=AdjustmentResult)
async def adjust_inventory(
    payload: InventoryAdjustRequest,
    db: AsyncSession = Depends(get_db),
):
    svc = InventoryService(db)
    record = await svc.adjust_stock_with_log(
        variant_id=payload.variant_id,
        warehouse_id=payload.warehouse_id,
        quantity_delta=payload.quantity_delta,
        reason=payload.reason,
        notes=payload.notes,
    )
    await db.commit()
    return AdjustmentResult(
        variant_id=record.variant_id,
        warehouse_id=record.warehouse_id,
        quantity_after=record.quantity,
    )


@router.post("/inventory/import-csv", response_model=BulkImportResult)
async def import_inventory_csv(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
):
    content = await file.read()
    svc = InventoryService(db)
    result = await svc.bulk_import_csv(content.decode("utf-8"))
    await db.commit()
    return BulkImportResult(**result)


@router.get("/inventory-report")
async def get_admin_inventory_report(
    warehouse_id: str | None = None,
    product_id: str | None = None,
    color: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Inventory listing report for admin — same as customer endpoint but no company_id required."""
    from app.models.inventory import InventoryRecord, Warehouse
    from app.models.product import Product, ProductVariant

    q = (
        select(
            ProductVariant.id.label("variant_id"),
            ProductVariant.sku,
            ProductVariant.color,
            ProductVariant.size,
            ProductVariant.sort_order,
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            Product.product_code.label("product_code"),
            Warehouse.id.label("warehouse_id"),
            Warehouse.name.label("warehouse_name"),
            InventoryRecord.quantity,
        )
        .join(Product, Product.id == ProductVariant.product_id)
        .join(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
        .join(Warehouse, Warehouse.id == InventoryRecord.warehouse_id)
        .where(ProductVariant.status == "active")
        .where(Product.status == "active")
        .where(Warehouse.is_active.is_(True))
    )

    if warehouse_id and warehouse_id != "all":
        q = q.where(Warehouse.id == warehouse_id)
    if product_id and product_id != "all":
        q = q.where(Product.id == product_id)
    if color and color != "all":
        q = q.where(ProductVariant.color == color)

    q = q.order_by(Product.name, ProductVariant.color, ProductVariant.sort_order, ProductVariant.size)
    rows = (await db.execute(q)).mappings().all()

    warehouses = (await db.execute(
        select(Warehouse).where(Warehouse.is_active.is_(True)).order_by(Warehouse.name)
    )).scalars().all()

    products = (await db.execute(
        select(Product.id, Product.name, Product.product_code)
        .where(Product.status == "active")
        .order_by(Product.name)
    )).all()

    colors = [r[0] for r in (await db.execute(
        select(ProductVariant.color)
        .where(ProductVariant.status == "active")
        .where(ProductVariant.color.isnot(None))
        .distinct()
        .order_by(ProductVariant.color)
    )).all() if r[0]]

    return {
        "items": [
            {
                "variant_id": str(r["variant_id"]),
                "sku": r["sku"],
                "product_id": str(r["product_id"]),
                "product_name": r["product_name"],
                "product_code": r["product_code"],
                "color": r["color"] or "—",
                "size": r["size"] or "—",
                "warehouse_id": str(r["warehouse_id"]),
                "warehouse_name": r["warehouse_name"],
                "available": int(r["quantity"]),
            }
            for r in rows
        ],
        "warehouses": [{"id": str(w.id), "name": w.name} for w in warehouses],
        "products": [{"id": str(p[0]), "name": p[1], "product_code": p[2]} for p in products],
        "colors": colors,
    }
