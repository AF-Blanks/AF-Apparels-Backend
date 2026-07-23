"""Admin — reporting & analytics endpoints."""
import csv
import io
from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.company import Company
from app.models.inventory import InventoryRecord
from app.models.order import Order, OrderItem
from app.models.product import Category, Product, ProductCategory, ProductVariant

router = APIRouter(prefix="/admin", tags=["Admin — Reports"])


def _date_range(period: str) -> tuple[datetime, datetime]:
    """Return (start, end) datetime for the given period key."""
    today = date.today()
    if period == "today":
        start = datetime.combine(today, datetime.min.time())
    elif period == "week":
        start = datetime.combine(today - timedelta(days=7), datetime.min.time())
    elif period == "month":
        start = datetime.combine(today - timedelta(days=30), datetime.min.time())
    elif period == "quarter":
        start = datetime.combine(today - timedelta(days=90), datetime.min.time())
    elif period == "year":
        start = datetime.combine(today - timedelta(days=365), datetime.min.time())
    else:
        start = datetime.combine(today - timedelta(days=30), datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    return start, end


# ── T185: Sales Report ────────────────────────────────────────────────────────

@router.get("/reports/sales")
async def sales_report(
    period: str = Query("month", description="today|week|month|quarter|year"),
    group_by: Literal["day", "week", "month"] = Query("day"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    start, end = _date_range(period)

    # Period data: revenue grouped by day/week/month
    if group_by == "day":
        trunc = func.date_trunc("day", Order.created_at)
    elif group_by == "week":
        trunc = func.date_trunc("week", Order.created_at)
    else:
        trunc = func.date_trunc("month", Order.created_at)

    period_q = (
        select(
            trunc.label("period"),
            func.count(Order.id).label("order_count"),
            func.sum(Order.total).label("revenue"),
            func.sum(Order.subtotal).label("subtotal"),
            func.sum(Order.shipping_cost).label("shipping"),
        )
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
        .group_by(trunc)
        .order_by(trunc)
    )
    period_rows = (await db.execute(period_q)).mappings().all()

    # By category: revenue per top-level category
    cat_q = (
        select(
            Category.name.label("category"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.count(OrderItem.id).label("items_sold"),
        )
        .select_from(Order)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(ProductVariant, ProductVariant.id == OrderItem.variant_id)
        .join(ProductCategory, ProductCategory.product_id == ProductVariant.product_id)
        .join(Category, Category.id == ProductCategory.category_id)
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
        .group_by(Category.name)
        .order_by(func.sum(OrderItem.line_total).desc())
        .limit(10)
    )
    cat_rows = (await db.execute(cat_q)).mappings().all()

    # Top products by revenue
    prod_q = (
        select(
            OrderItem.product_name,
            OrderItem.sku,
            func.sum(OrderItem.quantity).label("units_sold"),
            func.sum(OrderItem.line_total).label("revenue"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
        .group_by(OrderItem.product_name, OrderItem.sku)
        .order_by(func.sum(OrderItem.line_total).desc())
        .limit(20)
    )
    prod_rows = (await db.execute(prod_q)).mappings().all()

    # Summary totals
    total_q = (
        select(
            func.count(Order.id).label("total_orders"),
            func.sum(Order.total).label("total_revenue"),
            func.avg(Order.total).label("avg_order_value"),
        )
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
    )
    totals = (await db.execute(total_q)).mappings().one()

    return {
        "period": period,
        "group_by": group_by,
        "date_from": start.date().isoformat(),
        "date_to": end.date().isoformat(),
        "summary": {
            "total_orders": totals["total_orders"] or 0,
            "total_revenue": float(totals["total_revenue"] or 0),
            "avg_order_value": round(float(totals["avg_order_value"] or 0), 2),
        },
        "period_data": [
            {
                "period": str(r["period"])[:10] if r["period"] else None,
                "order_count": r["order_count"],
                "revenue": float(r["revenue"] or 0),
            }
            for r in period_rows
        ],
        "by_category": [
            {
                "category": r["category"],
                "revenue": float(r["revenue"] or 0),
                "items_sold": r["items_sold"],
            }
            for r in cat_rows
        ],
        "top_products": [
            {
                "product_name": r["product_name"],
                "sku": r["sku"],
                "units_sold": r["units_sold"],
                "revenue": float(r["revenue"] or 0),
            }
            for r in prod_rows
        ],
    }


# ── T186: Inventory Report ────────────────────────────────────────────────────

@router.get("/reports/inventory")
async def inventory_report(
    warehouse_id: str | None = Query(None),
    low_stock_only: bool = Query(False),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(
            ProductVariant.sku,
            func.concat_ws(" ", ProductVariant.color, ProductVariant.size).label("variant_name"),
            Product.name.label("product_name"),
            Product.product_code.label("product_code"),
            func.coalesce(func.sum(InventoryRecord.quantity), 0).label("quantity_on_hand"),
            func.coalesce(func.min(InventoryRecord.low_stock_threshold), 10).label("low_stock_threshold"),
        )
        .join(Product, Product.id == ProductVariant.product_id)
        .outerjoin(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
        .where(ProductVariant.status == "active")
        .group_by(
            ProductVariant.id,
            ProductVariant.sku,
            ProductVariant.color,
            ProductVariant.size,
            Product.name,
            Product.product_code,
        )
        .order_by(Product.name, ProductVariant.sku)
    )

    if warehouse_id:
        q = q.where(InventoryRecord.warehouse_id == warehouse_id)

    rows = (await db.execute(q)).mappings().all()

    items = []
    low_stock_items = []
    for r in rows:
        quantity_on_hand = int(r["quantity_on_hand"])
        threshold = r["low_stock_threshold"] or 10
        is_low = quantity_on_hand <= threshold
        item = {
            "sku": r["sku"],
            "product_name": r["product_name"],
            "product_code": r["product_code"],
            "variant_name": r["variant_name"],
            "quantity_on_hand": quantity_on_hand,
            "quantity_reserved": 0,
            "available": quantity_on_hand,
            "low_stock_threshold": threshold,
            "is_low_stock": is_low,
        }
        items.append(item)
        if is_low:
            low_stock_items.append(item)

    if low_stock_only:
        items = low_stock_items

    return {
        "total_skus": len(rows),
        "low_stock_count": len(low_stock_items),
        "items": items,
        "low_stock": low_stock_items[:50],
    }


# ── T187: Customer Report ─────────────────────────────────────────────────────

@router.get("/reports/customers")
async def customer_report(
    period: str = Query("month"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    start, end = _date_range(period)

    # New registrations over time
    reg_trunc = func.date_trunc("day", Company.created_at)
    reg_q = (
        select(
            reg_trunc.label("day"),
            func.count(Company.id).label("count"),
        )
        .where(Company.created_at.between(start, end))
        .group_by(reg_trunc)
        .order_by(reg_trunc)
    )
    reg_rows = (await db.execute(reg_q)).mappings().all()

    # Application approval stats for the period
    approval_q = (
        select(
            Company.status,
            func.count(Company.id).label("count"),
        )
        .where(Company.created_at.between(start, end))
        .group_by(Company.status)
    )
    approval_rows = (await db.execute(approval_q)).mappings().all()
    status_counts: dict[str, int] = {r["status"]: r["count"] for r in approval_rows}
    total_apps = sum(status_counts.values())
    approved = status_counts.get("active", 0)
    approval_rate = round((approved / total_apps * 100) if total_apps else 0, 1)

    # Avg order value by pricing tier
    aov_q = (
        select(
            Company.pricing_tier_id,
            func.avg(Order.total).label("avg_order_value"),
            func.count(Order.id).label("order_count"),
        )
        .join(Order, Order.company_id == Company.id)
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
        .group_by(Company.pricing_tier_id)
    )
    aov_rows = (await db.execute(aov_q)).mappings().all()

    # Top customers by spend
    top_q = (
        select(
            Company.name.label("company_name"),
            func.count(Order.id).label("order_count"),
            func.sum(Order.total).label("total_spend"),
        )
        .join(Order, Order.company_id == Company.id)
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
        .group_by(Company.id, Company.name)
        .order_by(func.sum(Order.total).desc())
        .limit(10)
    )
    top_rows = (await db.execute(top_q)).mappings().all()

    return {
        "period": period,
        "date_from": start.date().isoformat(),
        "date_to": end.date().isoformat(),
        "registrations_trend": [
            {"day": str(r["day"])[:10], "count": r["count"]}
            for r in reg_rows
        ],
        "approval_rate": approval_rate,
        "status_breakdown": status_counts,
        "aov_by_tier": [
            {
                "pricing_tier_id": str(r["pricing_tier_id"]) if r["pricing_tier_id"] else None,
                "avg_order_value": round(float(r["avg_order_value"] or 0), 2),
                "order_count": r["order_count"],
            }
            for r in aov_rows
        ],
        "top_customers": [
            {
                "company_name": r["company_name"],
                "order_count": r["order_count"],
                "total_spend": float(r["total_spend"] or 0),
            }
            for r in top_rows
        ],
    }


@router.get("/reports/variant-sales")
async def variant_sales_report(
    period: str = Query("week", description="today|week|month|quarter|year"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Sales broken down by product → color → size for a given period."""
    from collections import defaultdict

    start, end = _date_range(period)

    rows = (await db.execute(
        select(
            OrderItem.product_name,
            OrderItem.color,
            OrderItem.size,
            func.sum(OrderItem.quantity).label("units_sold"),
            func.sum(OrderItem.line_total).label("revenue"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
        .group_by(OrderItem.product_name, OrderItem.color, OrderItem.size)
        .order_by(OrderItem.product_name, OrderItem.color, OrderItem.size)
    )).all()

    # Group into product → variants
    products: dict = defaultdict(lambda: {"product_name": "", "total_units": 0, "total_revenue": 0.0, "variants": []})
    for r in rows:
        key = r.product_name or "—"
        products[key]["product_name"] = key
        products[key]["total_units"] += int(r.units_sold)
        products[key]["total_revenue"] += float(r.revenue or 0)
        products[key]["variants"].append({
            "color": r.color or "—",
            "size": r.size or "—",
            "units_sold": int(r.units_sold),
            "revenue": float(r.revenue or 0),
        })

    return {
        "period": period,
        "date_from": start.date().isoformat(),
        "date_to": end.date().isoformat(),
        "products": list(products.values()),
        "summary": {
            "total_products": len(products),
            "total_units": sum(p["total_units"] for p in products.values()),
            "total_revenue": sum(p["total_revenue"] for p in products.values()),
        },
    }


# ── Customer Purchase History ─────────────────────────────────────────────────

@router.get("/reports/customer-purchase-history")
async def customer_purchase_history(
    company_id: str = Query(..., description="Company UUID to pull history for"),
    year: int | None = Query(None),
    display: str = Query("product", description="product | price"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin view of a specific customer's purchase history (same logic as /account/sales-history)."""
    import uuid as _uuid
    from collections import defaultdict
    from sqlalchemy import extract

    try:
        company_uuid = _uuid.UUID(str(company_id))
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Invalid company_id")

    q = (
        select(OrderItem, Order.created_at, Order.order_number)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.company_id == company_uuid)
        .where(Order.status.notin_(["cancelled", "refunded"]))
    )
    if year:
        q = q.where(extract("year", Order.created_at) == year)
    q = q.order_by(OrderItem.product_name, Order.created_at)
    rows = (await db.execute(q)).all()

    if display == "price":
        result_items = [
            {
                "order_number": r[2],
                "product_name": r[0].product_name,
                "sku": r[0].sku,
                "color": r[0].color or "—",
                "size": r[0].size or "—",
                "quantity": r[0].quantity,
                "unit_price": float(r[0].unit_price),
                "line_total": float(r[0].line_total),
                "ordered_at": r[1].isoformat() if r[1] else None,
            }
            for r in rows
        ]
    else:
        grouped: dict = defaultdict(lambda: {"product_name": "", "units_sold": 0, "total_revenue": 0.0, "_variants": {}})
        for r in rows:
            key = r[0].product_name
            grouped[key]["product_name"] = r[0].product_name
            grouped[key]["units_sold"] += r[0].quantity
            grouped[key]["total_revenue"] += float(r[0].line_total)
            vkey = f"{r[0].color or '—'} / {r[0].size or '—'}"
            if vkey not in grouped[key]["_variants"]:
                grouped[key]["_variants"][vkey] = {"color": r[0].color or "—", "size": r[0].size or "—", "units_sold": 0, "total_revenue": 0.0}
            grouped[key]["_variants"][vkey]["units_sold"] += r[0].quantity
            grouped[key]["_variants"][vkey]["total_revenue"] += float(r[0].line_total)
        result_items = []
        for g in grouped.values():
            variants = sorted(g.pop("_variants").values(), key=lambda v: (v["color"], v["size"]))
            result_items.append({**g, "variants": variants})

    return {"items": result_items, "year": year, "display": display}


# ── T188: CSV Export ──────────────────────────────────────────────────────────

@router.get("/reports/{report_type}/export-csv")
async def export_report_csv(
    report_type: Literal["sales", "inventory", "customers"],
    period: str = Query("month"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    output = io.StringIO()
    writer = csv.writer(output)
    start, end = _date_range(period)

    if report_type == "sales":
        writer.writerow(["Date", "Order Count", "Revenue"])
        q = (
            select(
                func.date_trunc("day", Order.created_at).label("day"),
                func.count(Order.id).label("cnt"),
                func.sum(Order.total).label("rev"),
            )
            .where(Order.created_at.between(start, end))
            .where(Order.status.notin_(["cancelled", "refunded"]))
            .group_by(func.date_trunc("day", Order.created_at))
            .order_by(func.date_trunc("day", Order.created_at))
        )
        for r in (await db.execute(q)).mappings().all():
            writer.writerow([str(r["day"])[:10], r["cnt"], float(r["rev"] or 0)])

    elif report_type == "inventory":
        writer.writerow(["SKU", "Product Code", "Product", "Variant", "On Hand", "Available", "Low Stock"])
        q = (
            select(
                ProductVariant.sku,
                Product.name.label("product_name"),
                Product.product_code.label("product_code"),
                func.concat_ws(" ", ProductVariant.color, ProductVariant.size).label("variant_name"),
                func.coalesce(func.sum(InventoryRecord.quantity), 0).label("on_hand"),
                func.coalesce(func.min(InventoryRecord.low_stock_threshold), 10).label("low_stock_threshold"),
            )
            .join(Product, Product.id == ProductVariant.product_id)
            .outerjoin(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
            .where(ProductVariant.status == "active")
            .group_by(ProductVariant.id, ProductVariant.sku, ProductVariant.color, ProductVariant.size, Product.name, Product.product_code)
        )
        for r in (await db.execute(q)).mappings().all():
            on_hand = int(r["on_hand"])
            threshold = r["low_stock_threshold"] or 10
            writer.writerow([r["sku"], r["product_code"] or "", r["product_name"], r["variant_name"], on_hand, on_hand, "Yes" if on_hand <= threshold else "No"])

    elif report_type == "customers":
        writer.writerow(["Company", "Order Count", "Total Spend"])
        q = (
            select(
                Company.name,
                func.count(Order.id).label("cnt"),
                func.sum(Order.total).label("spend"),
            )
            .join(Order, Order.company_id == Company.id)
            .where(Order.created_at.between(start, end))
            .where(Order.status.notin_(["cancelled", "refunded"]))
            .group_by(Company.id, Company.name)
            .order_by(func.sum(Order.total).desc())
        )
        for r in (await db.execute(q)).mappings().all():
            writer.writerow([r["name"], r["cnt"], float(r["spend"] or 0)])

    output.seek(0)
    filename = f"{report_type}-report-{period}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
