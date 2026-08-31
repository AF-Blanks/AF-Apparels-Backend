"""Admin — reporting & analytics endpoints."""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.company import Company
from app.models.inventory import InventoryRecord
from app.models.order import Order, OrderItem
from app.models.product import Category, Product, ProductCategory, ProductVariant

router = APIRouter(prefix="/admin", tags=["Admin — Reports"])


def _date_range(
    period: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[datetime, datetime]:
    """Return (start, end) datetime for the given period key.

    An explicit date_from/date_to wins over the rolling period, so reports can be
    run for any exact span (a single day, one week, a month, a full year) instead
    of only the fixed "last N days" windows.
    """
    if date_from or date_to:
        today_ = date.today()
        f = date_from or date(2000, 1, 1)
        t = date_to or today_
        if f > t:
            f, t = t, f
        return (
            datetime.combine(f, datetime.min.time()),
            datetime.combine(t, datetime.max.time()),
        )

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
    group_by: Literal["day", "week", "month", "year"] = Query("day"),
    date_from: date | None = Query(None, description="Exact start date (overrides period)"),
    date_to: date | None = Query(None, description="Exact end date (overrides period)"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    start, end = _date_range(period, date_from, date_to)

    # Period data: revenue grouped by day/week/month/year
    if group_by == "day":
        trunc = func.date_trunc("day", Order.created_at)
    elif group_by == "week":
        trunc = func.date_trunc("week", Order.created_at)
    elif group_by == "year":
        trunc = func.date_trunc("year", Order.created_at)
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
            func.sum(OrderItem.quantity).label("units_sold"),
            func.sum(OrderItem.line_total).label("revenue"),
            func.count(func.distinct(OrderItem.sku)).label("variant_count"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.created_at.between(start, end))
        .where(Order.status.notin_(["cancelled", "refunded"]))
        # Aggregate per PRODUCT (all its variants) so the ranking is 20 products,
        # not 20 individual size/colour rows — accurate, nothing dropped.
        .group_by(OrderItem.product_name)
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

    # Net out refunds issued (approved RMAs that refunded to the card) on orders
    # in this period, so "Total Sales" reflects NET sales — the same figure
    # QuickBooks shows once a credit memo reverses the invoice. Money given back
    # is not revenue.
    from app.models.rma import RMARequest
    refund_total = (await db.execute(
        select(func.coalesce(func.sum(RMARequest.refund_amount), 0))
        .select_from(RMARequest)
        .join(Order, RMARequest.order_id == Order.id)
        .where(
            Order.created_at.between(start, end),
            RMARequest.status == "approved",
            RMARequest.refund_status == "refunded",
        )
    )).scalar() or 0
    gross_revenue = float(totals["total_revenue"] or 0)
    net_revenue = max(0.0, gross_revenue - float(refund_total))

    # Fully-returned orders (approved refunds covering the whole product
    # subtotal) shouldn't count as sales orders — same reasoning as netting
    # revenue. Partial returns still count (the customer kept part of the order).
    fully_returned_q = (
        select(Order.id)
        .join(RMARequest, RMARequest.order_id == Order.id)
        .where(
            Order.created_at.between(start, end),
            Order.status.notin_(["cancelled", "refunded"]),
            RMARequest.status == "approved",
            RMARequest.refund_status == "refunded",
        )
        .group_by(Order.id, Order.subtotal)
        .having(func.coalesce(func.sum(RMARequest.refund_amount), 0) >= Order.subtotal)
    )
    fully_returned_count = len((await db.execute(fully_returned_q)).all())
    gross_orders = int(totals["total_orders"] or 0)
    net_orders = max(0, gross_orders - fully_returned_count)
    net_aov = (net_revenue / net_orders) if net_orders else 0.0

    return {
        "period": period,
        "group_by": group_by,
        "date_from": start.date().isoformat(),
        "date_to": end.date().isoformat(),
        "summary": {
            "total_orders": net_orders,
            "gross_orders": gross_orders,
            "total_revenue": net_revenue,
            "gross_revenue": round(gross_revenue, 2),
            "total_refunds": round(float(refund_total), 2),
            "avg_order_value": round(net_aov, 2),
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
                "variant_count": int(r["variant_count"] or 0),
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


@router.get("/reports/inventory-value")
async def inventory_value_report(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Total worth of stock currently on hand: for each product, quantity × unit
    cost, plus a grand total. Answers 'how much money is sitting in my inventory
    right now?'. Uses each variant's cost_per_item and stock summed across
    warehouses. Variants that have stock but no cost on file are counted separately
    so the owner knows the valuation excludes them."""
    variant_q = (
        select(
            Product.id.label("product_id"),
            Product.name.label("product_name"),
            ProductVariant.cost_per_item.label("cost"),
            func.coalesce(func.sum(InventoryRecord.quantity), 0).label("qty"),
        )
        .join(Product, Product.id == ProductVariant.product_id)
        .outerjoin(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
        .where(ProductVariant.status == "active")
        .group_by(Product.id, Product.name, ProductVariant.id, ProductVariant.cost_per_item)
    )
    rows = (await db.execute(variant_q)).mappings().all()

    products: dict[str, dict] = {}
    total_value = 0.0
    total_units = 0
    missing_cost_units = 0
    missing_cost_skus = 0
    for r in rows:
        qty = int(r["qty"] or 0)
        if qty <= 0:
            continue
        total_units += qty
        pid = str(r["product_id"])
        p = products.setdefault(pid, {"product_name": r["product_name"], "quantity": 0, "value": 0.0, "fully_costed": True})
        p["quantity"] += qty
        if r["cost"] is None:
            missing_cost_units += qty
            missing_cost_skus += 1
            p["fully_costed"] = False
        else:
            val = qty * float(r["cost"])
            p["value"] += val
            total_value += val

    items = sorted(products.values(), key=lambda x: x["value"], reverse=True)
    for p in items:
        p["value"] = round(p["value"], 2)

    return {
        "total_value": round(total_value, 2),
        "total_units": total_units,
        "product_count": len(items),
        "missing_cost_units": missing_cost_units,
        "missing_cost_skus": missing_cost_skus,
        "items": items,
    }


@router.get("/reports/outstanding")
async def outstanding_report(
    include_settled: bool = Query(False, description="Also list customers who owe nothing"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Accounts receivable by customer — who owes money, how much, and how old it is.

    Everything comes from our own orders table in a single grouped query: no
    QuickBooks calls at all, so this can be opened as often as needed without
    touching the QB API budget. (The per-customer "Refresh from QuickBooks" button
    on the customer page remains the only QB lookup, and it is one manual call.)
    """
    from sqlalchemy import and_

    # payment_status is the source of truth for whether an order is settled:
    # card-captured orders set payment_status="paid" without always filling in
    # amount_paid, so "total - amount_paid" would wrongly report a fully paid
    # order as owing its whole value. amount_paid is only used to work out how
    # much of a still-open order has been part-paid.
    _settled = Order.payment_status.in_(["paid", "refunded"])
    owed = case(
        (_settled, 0),
        else_=func.greatest(Order.total - func.coalesce(Order.amount_paid, 0), 0),
    )
    paid_amount = case(
        (_settled, Order.total),
        else_=func.coalesce(Order.amount_paid, 0),
    )

    now = datetime.now(timezone.utc)
    d30, d60, d90 = now - timedelta(days=30), now - timedelta(days=60), now - timedelta(days=90)

    def _bucket(condition):
        return func.coalesce(func.sum(case((condition, owed), else_=0)), 0)

    q = (
        select(
            Company.id.label("company_id"),
            Company.name.label("company_name"),
            Company.company_email.label("email"),
            Company.phone.label("phone"),
            Company.net30_enabled.label("net30"),
            Company.net7_enabled.label("net7"),
            func.count(Order.id).label("order_count"),
            func.coalesce(func.sum(Order.total), 0).label("total_purchased"),
            func.coalesce(func.sum(paid_amount), 0).label("total_paid"),
            func.coalesce(func.sum(owed), 0).label("outstanding"),
            func.count(case((owed > 0, Order.id))).label("unpaid_orders"),
            func.min(case((owed > 0, Order.created_at))).label("oldest_unpaid_at"),
            # Ageing: split what's owed by how old the order is.
            _bucket(Order.created_at >= d30).label("age_current"),
            _bucket(and_(Order.created_at < d30, Order.created_at >= d60)).label("age_30"),
            _bucket(and_(Order.created_at < d60, Order.created_at >= d90)).label("age_60"),
            _bucket(Order.created_at < d90).label("age_90"),
        )
        .join(Order, Order.company_id == Company.id)
        .where(Order.status.notin_(["cancelled", "refunded"]))
        .group_by(
            Company.id, Company.name, Company.company_email, Company.phone,
            Company.net30_enabled, Company.net7_enabled,
        )
        .order_by(func.coalesce(func.sum(owed), 0).desc())
    )
    if not include_settled:
        q = q.having(func.coalesce(func.sum(owed), 0) > 0)

    rows = (await db.execute(q)).mappings().all()

    items = []
    for r in rows:
        oldest = r["oldest_unpaid_at"]
        # created_at is stored tz-aware; guard anyway so one legacy naive row
        # can't break the whole report.
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        days = (now - oldest).days if oldest else None
        terms = "Net 7" if r["net7"] else ("Net 30" if r["net30"] else None)
        items.append({
            "company_id": str(r["company_id"]),
            "company_name": r["company_name"],
            "email": r["email"],
            "phone": r["phone"],
            "payment_terms": terms,
            "order_count": r["order_count"],
            "unpaid_orders": r["unpaid_orders"],
            "total_purchased": round(float(r["total_purchased"] or 0), 2),
            "total_paid": round(float(r["total_paid"] or 0), 2),
            "outstanding": round(float(r["outstanding"] or 0), 2),
            "oldest_unpaid_date": oldest.date().isoformat() if oldest else None,
            "days_outstanding": days,
            "aging": {
                "current": round(float(r["age_current"] or 0), 2),
                "d30": round(float(r["age_30"] or 0), 2),
                "d60": round(float(r["age_60"] or 0), 2),
                "d90": round(float(r["age_90"] or 0), 2),
            },
        })

    return {
        "customers_owing": sum(1 for i in items if i["outstanding"] > 0.005),
        "total_outstanding": round(sum(i["outstanding"] for i in items), 2),
        "total_aging": {
            "current": round(sum(i["aging"]["current"] for i in items), 2),
            "d30": round(sum(i["aging"]["d30"] for i in items), 2),
            "d60": round(sum(i["aging"]["d60"] for i in items), 2),
            "d90": round(sum(i["aging"]["d90"] for i in items), 2),
        },
        "items": items,
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

    # Top customers by spend — with what they've paid vs. what's still owed
    # (all consistent within the selected period, so the columns tie out).
    # payment_status is the source of truth for settled orders — see
    # outstanding_report: card captures set it without always filling amount_paid.
    _c_settled = Order.payment_status.in_(["paid", "refunded"])
    _c_paid = case((_c_settled, Order.total), else_=func.coalesce(Order.amount_paid, 0))
    _c_owed = case(
        (_c_settled, 0),
        else_=func.greatest(Order.total - func.coalesce(Order.amount_paid, 0), 0),
    )
    top_q = (
        select(
            Company.name.label("company_name"),
            func.count(Order.id).label("order_count"),
            func.sum(Order.total).label("total_spend"),
            func.coalesce(func.sum(_c_paid), 0).label("total_paid"),
            func.coalesce(func.sum(_c_owed), 0).label("outstanding"),
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
                "total_paid": float(r["total_paid"] or 0),
                "outstanding_balance": float(r["outstanding"] or 0),
            }
            for r in top_rows
        ],
    }


# ── Variant Sales Report ──────────────────────────────────────────────────────

@router.get("/reports/variant-sales")
async def variant_sales_report(
    period: str = Query("week", description="today|week|month|quarter|year"),
    date_from: date | None = Query(None, description="Exact start date (overrides period)"),
    date_to: date | None = Query(None, description="Exact end date (overrides period)"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Sales broken down by product → color → size for a given period."""
    from collections import defaultdict

    start, end = _date_range(period, date_from, date_to)

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


# ── QuickBooks reconciliation ─────────────────────────────────────────────────
@router.get("/reports/qb-reconciliation")
async def qb_reconciliation(
    period: str = Query("month"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Line the dashboard's sales figure up against QuickBooks income, order by order.

    The two are *meant* to differ by the sales tax: the dashboard reports what we
    billed (order.total = subtotal - discount + shipping + tax + convenience fee)
    while a P&L reports income, and collected tax is booked to the Sales Tax
    Payable *liability*, never to income. Whatever is left after subtracting tax
    is a real gap — an order whose invoice never reached QB, or one whose invoice
    carries a TxnDate outside the window being compared — and this names it.

    Costs at most a handful of QB calls: one paged query over the window, plus one
    batched DocNumber lookup for whatever did not match.
    """
    import asyncio

    from app.api.v1.admin.analytics import ACTIVE_STATUSES
    from app.services.quickbooks_service import QuickBooksService

    start, end = _date_range(period, date_from, date_to)

    rows = (await db.execute(
        select(
            Order.order_number,
            Order.created_at,
            Order.total,
            Order.tax_amount,
            Order.shipping_cost,
            Order.payment_status,
            Order.status,
            Order.qb_invoice_id,
            Company.name.label("company_name"),
        )
        .join(Company, Order.company_id == Company.id, isouter=True)
        .where(
            Order.created_at >= start,
            Order.created_at <= end,
            Order.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Order.created_at.desc())
    )).all()

    app_total = sum(float(r.total or 0) for r in rows)
    app_tax = sum(float(r.tax_amount or 0) for r in rows)

    # ── Pull the QB invoices that fall inside the same window ─────────────────
    by_id: dict[str, dict] = {}
    by_doc: dict[str, dict] = {}
    qb_error: str | None = None
    svc = None
    d_from, d_to = start.date().isoformat(), end.date().isoformat()

    try:
        svc = await QuickBooksService().initialize()
        pos = 1
        while True:
            soql = (
                "SELECT Id, DocNumber, TxnDate, TotalAmt, Balance FROM Invoice "
                f"WHERE TxnDate >= '{d_from}' AND TxnDate <= '{d_to}' "
                f"STARTPOSITION {pos} MAXRESULTS 500"
            )
            resp = await asyncio.to_thread(svc.query, soql)
            batch = (resp.get("QueryResponse") or {}).get("Invoice") or []
            for inv in batch:
                inv["_in_window"] = True
                by_id[str(inv.get("Id"))] = inv
                if inv.get("DocNumber"):
                    by_doc[str(inv["DocNumber"])] = inv
            if len(batch) < 500:
                break
            pos += 500
    except Exception as exc:  # QB down / disconnected — still return the app side
        qb_error = str(exc)[:300]

    # ── Anything unmatched may still exist in QB under a different TxnDate ────
    if svc is not None and qb_error is None:
        unmatched = [
            r.order_number for r in rows
            if not (
                (r.qb_invoice_id and str(r.qb_invoice_id) in by_id)
                or str(r.order_number) in by_doc
            )
        ]
        try:
            for i in range(0, len(unmatched), 40):
                chunk = unmatched[i:i + 40]
                in_list = ", ".join("'" + svc._soql_escape(d) + "'" for d in chunk)
                resp = await asyncio.to_thread(
                    svc.query,
                    "SELECT Id, DocNumber, TxnDate, TotalAmt, Balance FROM Invoice "
                    f"WHERE DocNumber IN ({in_list}) MAXRESULTS 500",
                )
                for inv in (resp.get("QueryResponse") or {}).get("Invoice") or []:
                    inv["_in_window"] = False
                    by_id.setdefault(str(inv.get("Id")), inv)
                    if inv.get("DocNumber"):
                        by_doc.setdefault(str(inv["DocNumber"]), inv)
        except Exception as exc:
            qb_error = str(exc)[:300]

    # ── Match each order to its invoice ───────────────────────────────────────
    matched_qb_ids: set[str] = set()
    out: list[dict] = []
    missing_total, missing_n = 0.0, 0
    outside_total, outside_n = 0.0, 0
    mismatch_total, mismatch_n = 0.0, 0
    qb_window_total = 0.0

    for r in rows:
        total = float(r.total or 0)
        tax = float(r.tax_amount or 0)
        inv = None
        if r.qb_invoice_id and str(r.qb_invoice_id) in by_id:
            inv = by_id[str(r.qb_invoice_id)]
        elif str(r.order_number) in by_doc:
            inv = by_doc[str(r.order_number)]

        qb_total = None
        txn_date = None
        if inv is None:
            state = "missing_from_qb"
            missing_total += total - tax
            missing_n += 1
        else:
            matched_qb_ids.add(str(inv.get("Id")))
            qb_total = float(inv.get("TotalAmt") or 0)
            txn_date = inv.get("TxnDate")
            if not inv.get("_in_window"):
                state = "dated_outside_range"
                outside_total += total - tax
                outside_n += 1
            elif abs(qb_total - total) > 0.01:
                state = "amount_mismatch"
                mismatch_total += qb_total - total
                mismatch_n += 1
                qb_window_total += qb_total
            else:
                state = "ok"
                qb_window_total += qb_total

        out.append({
            "order_number": r.order_number,
            "date": r.created_at.isoformat() if r.created_at else None,
            "company": r.company_name,
            "status": r.status,
            "payment_status": r.payment_status,
            "app_total": round(total, 2),
            "sales_tax": round(tax, 2),
            "shipping": round(float(r.shipping_cost or 0), 2),
            "counts_as_income": round(total - tax, 2),
            "qb_invoice_id": str(r.qb_invoice_id) if r.qb_invoice_id else None,
            "qb_doc_number": inv.get("DocNumber") if inv else None,
            "qb_total": round(qb_total, 2) if qb_total is not None else None,
            "qb_txn_date": txn_date,
            "state": state,
        })

    # Invoices sitting in QB for this window that no order accounts for.
    extra = [
        {
            "qb_invoice_id": str(inv.get("Id")),
            "qb_doc_number": inv.get("DocNumber"),
            "qb_txn_date": inv.get("TxnDate"),
            "qb_total": round(float(inv.get("TotalAmt") or 0), 2),
        }
        for inv in by_id.values()
        if inv.get("_in_window") and str(inv.get("Id")) not in matched_qb_ids
    ]
    extra_total = sum(e["qb_total"] for e in extra)

    return {
        "period": {"from": start.date().isoformat(), "to": end.date().isoformat()},
        "qb_available": qb_error is None,
        "qb_error": qb_error,
        "summary": {
            "orders": len(rows),
            # What the dashboard tile shows.
            "dashboard_sales": round(app_total, 2),
            # Removed because QB books it to a liability, not to income.
            "sales_tax_excluded": round(app_tax, 2),
            # What a QB P&L for this window *should* report as income.
            "expected_qb_income": round(app_total - app_tax, 2),
            "qb_invoice_total_in_window": round(qb_window_total, 2),
            "missing_from_qb_count": missing_n,
            "missing_from_qb_income": round(missing_total, 2),
            "dated_outside_range_count": outside_n,
            "dated_outside_range_income": round(outside_total, 2),
            "amount_mismatch_count": mismatch_n,
            "amount_mismatch_delta": round(mismatch_total, 2),
            "extra_in_qb_count": len(extra),
            "extra_in_qb_total": round(extra_total, 2),
        },
        "orders": out,
        "extra_invoices": extra,
    }


# ── Inventory vs QuickBooks reconciliation ────────────────────────────────────
@router.get("/reports/inventory-qb-reconciliation")
async def inventory_qb_reconciliation(
    only_problems: bool = Query(True, description="Omit variants that already agree"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Compare our stock against QuickBooks' QtyOnHand, variant by variant.

    Two systems counting the same shelf drift apart, and until now the only sign
    was a negative quantity turning up on a QuickBooks worksheet weeks later. This
    names every variant where the two disagree, so a stock-count reload can be
    checked the same day it is loaded.

    Cheap on the API: QuickBooks items are read in pages of 1000 (three calls for
    a three-thousand-item catalogue), not one call per variant, and the read goes
    through the same 250/min limiter as everything else.
    """
    import asyncio

    from app.services.quickbooks_service import QuickBooksService

    # ── Our side: stock per variant ───────────────────────────────────────────
    rows = (await db.execute(
        select(
            ProductVariant.id,
            ProductVariant.sku,
            ProductVariant.color,
            ProductVariant.size,
            ProductVariant.qb_item_id,
            Product.name.label("product_name"),
            func.coalesce(func.sum(InventoryRecord.quantity), 0).label("app_stock"),
        )
        .join(Product, Product.id == ProductVariant.product_id)
        .outerjoin(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
        .group_by(
            ProductVariant.id, ProductVariant.sku, ProductVariant.color,
            ProductVariant.size, ProductVariant.qb_item_id, Product.name,
        )
        .order_by(Product.name, ProductVariant.color, ProductVariant.size)
    )).all()

    # ── QuickBooks side: QtyOnHand per item, in pages ─────────────────────────
    qb_qty: dict[str, float] = {}
    qb_error: str | None = None
    try:
        svc = await QuickBooksService().initialize()
        pos = 1
        while True:
            resp = await asyncio.to_thread(
                svc.query,
                "SELECT Id, QtyOnHand FROM Item WHERE Type = 'Inventory' "
                f"STARTPOSITION {pos} MAXRESULTS 1000",
            )
            batch = (resp.get("QueryResponse") or {}).get("Item") or []
            for it in batch:
                qb_qty[str(it.get("Id"))] = float(it.get("QtyOnHand") or 0)
            if len(batch) < 1000:
                break
            pos += 1000
    except Exception as exc:
        qb_error = str(exc)[:300]

    out: list[dict] = []
    n_ok = n_mismatch = n_negative = n_not_in_qb = 0
    app_units = qb_units = 0.0
    short_by = 0.0

    for r in rows:
        app_stock = float(r.app_stock or 0)
        app_units += app_stock
        item_id = str(r.qb_item_id) if r.qb_item_id else None

        if not item_id:
            # Never synced, so QuickBooks has no line for it at all. Its sales fall
            # back to a generic service item, which is why these never show up as
            # negative — they are invisible instead.
            state, qty, diff = "not_in_qb", None, None
            n_not_in_qb += 1
        elif qb_error is not None or item_id not in qb_qty:
            state, qty, diff = "unknown", None, None
        else:
            qty = qb_qty[item_id]
            qb_units += qty
            diff = round(qty - app_stock, 2)
            if qty < 0:
                state = "negative_in_qb"
                n_negative += 1
            elif abs(diff) > 0.001:
                state = "mismatch"
                n_mismatch += 1
            else:
                state = "ok"
                n_ok += 1
            if diff < 0:
                short_by += -diff

        if only_problems and state == "ok":
            continue
        out.append({
            "sku": r.sku,
            "product": r.product_name,
            "color": r.color,
            "size": r.size,
            "app_stock": app_stock,
            "qb_qty": qty,
            "difference": diff,
            "qb_item_id": item_id,
            "state": state,
        })

    return {
        "qb_available": qb_error is None,
        "qb_error": qb_error,
        "summary": {
            "variants": len(rows),
            "agreeing": n_ok,
            "mismatched": n_mismatch,
            "negative_in_qb": n_negative,
            "not_in_qb": n_not_in_qb,
            "app_units": round(app_units, 2),
            "qb_units": round(qb_units, 2),
            # How many units QuickBooks is short across every variant it knows —
            # the size of the hole, in pieces.
            "qb_short_by_units": round(short_by, 2),
        },
        "variants": out,
    }


# ── Variant Sales Comparison ──────────────────────────────────────────────────

# The shop opened on this date. Nothing before it is real trading, so month
# pickers start here and a comparison against an earlier month is meaningless.
STORE_LAUNCH = date(2026, 8, 1)


def _month_bounds(ym: str) -> tuple[datetime, datetime, str]:
    """Turn "2026-09" into the datetimes covering it, plus a readable label."""
    try:
        year, month = (int(p) for p in ym.split("-", 1))
        first = date(year, month, 1)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Month must look like 2026-09")
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    return (
        datetime.combine(first, datetime.min.time()),
        datetime.combine(nxt - timedelta(days=1), datetime.max.time()),
        first.strftime("%B %Y"),
    )


def _previous_month(ym: str) -> str:
    year, month = (int(p) for p in ym.split("-", 1))
    return f"{year - (month == 1)}-{(month - 2) % 12 + 1:02d}"


@router.get("/reports/variant-sales-comparison")
async def variant_sales_comparison(
    month: str | None = Query(None, description='Month to report, as "2026-09". Defaults to the current month.'),
    compare_to: str | None = Query(None, description='Month to compare against. Defaults to the month before.'),
    q: str | None = Query(None, description="Filter by product name, colour or size"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """One month's sales against another, down to the individual variant.

    "How did 1001 Pink 3XL do this month against last?" could only be answered by
    running the variant report twice and comparing by eye. This puts both months
    on one row, with the change between them, so a colour or size that has fallen
    away is visible rather than inferred.

    Months start at the shop's opening date — there is nothing before it to
    compare against.
    """
    from collections import defaultdict

    today = date.today()
    month = month or today.strftime("%Y-%m")
    compare_to = compare_to or _previous_month(month)

    cur_start, cur_end, cur_label = _month_bounds(month)
    prv_start, prv_end, prv_label = _month_bounds(compare_to)

    async def _sold(start: datetime, end: datetime) -> dict[tuple, tuple[int, float]]:
        stmt = (
            select(
                OrderItem.product_name,
                OrderItem.color,
                OrderItem.size,
                func.coalesce(func.sum(OrderItem.quantity), 0).label("units"),
                func.coalesce(func.sum(OrderItem.line_total), 0).label("revenue"),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.created_at.between(start, end))
            .where(Order.status.notin_(["cancelled", "refunded"]))
            .group_by(OrderItem.product_name, OrderItem.color, OrderItem.size)
        )
        if q and q.strip():
            # Same shape as the product search: every word must match somewhere, so
            # "1001 pink" narrows to the pink variants of 1001 rather than to
            # anything mentioning either.
            for tok in (t for t in q.split() if t.strip()):
                like = f"%{tok.strip()}%"
                stmt = stmt.where(
                    or_(
                        OrderItem.product_name.ilike(like),
                        OrderItem.color.ilike(like),
                        OrderItem.size.ilike(like),
                    )
                )
        return {
            (r.product_name or "—", r.color or "—", r.size or "—"): (int(r.units or 0), float(r.revenue or 0))
            for r in (await db.execute(stmt)).all()
        }

    current = await _sold(cur_start, cur_end)
    previous = await _sold(prv_start, prv_end)

    def _pct(now: float, before: float) -> float | None:
        # No percentage from a base of nothing — "up 100%" from zero reads as a
        # modest gain when it is actually a variant that only just started selling.
        if before == 0:
            return None
        return round((now - before) / before * 100, 1)

    rows: list[dict] = []
    for key in set(current) | set(previous):
        name, color, size = key
        units, revenue = current.get(key, (0, 0.0))
        p_units, p_revenue = previous.get(key, (0, 0.0))
        if units and not p_units:
            state = "new"
        elif p_units and not units:
            state = "stopped"
        elif units > p_units:
            state = "up"
        elif units < p_units:
            state = "down"
        else:
            state = "same"
        rows.append({
            "product_name": name,
            "color": color,
            "size": size,
            "units": units,
            "prev_units": p_units,
            "units_change": units - p_units,
            "units_change_pct": _pct(units, p_units),
            "revenue": round(revenue, 2),
            "prev_revenue": round(p_revenue, 2),
            "revenue_change": round(revenue - p_revenue, 2),
            "revenue_change_pct": _pct(revenue, p_revenue),
            "state": state,
        })

    # Biggest movers first, in either direction — those are what a buyer acts on.
    rows.sort(key=lambda r: (-abs(r["units_change"]), r["product_name"], r["color"], r["size"]))

    # Months the shop has actually traded in, newest first, for the pickers.
    months: list[dict] = []
    cursor = date(today.year, today.month, 1)
    while cursor >= STORE_LAUNCH:
        months.append({"value": cursor.strftime("%Y-%m"), "label": cursor.strftime("%B %Y")})
        cursor = date(cursor.year - (cursor.month == 1), (cursor.month - 2) % 12 + 1, 1)

    units_now = sum(r["units"] for r in rows)
    units_before = sum(r["prev_units"] for r in rows)
    rev_now = sum(r["revenue"] for r in rows)
    rev_before = sum(r["prev_revenue"] for r in rows)

    return {
        "period": {"value": month, "label": cur_label},
        "compare": {"value": compare_to, "label": prv_label},
        "available_months": months,
        "summary": {
            "variants": len(rows),
            "units": units_now,
            "prev_units": units_before,
            "units_change": units_now - units_before,
            "units_change_pct": _pct(units_now, units_before),
            "revenue": round(rev_now, 2),
            "prev_revenue": round(rev_before, 2),
            "revenue_change": round(rev_now - rev_before, 2),
            "revenue_change_pct": _pct(rev_now, rev_before),
            "improved": sum(1 for r in rows if r["state"] in ("up", "new")),
            "declined": sum(1 for r in rows if r["state"] in ("down", "stopped")),
        },
        "rows": rows,
    }


# ── Stock Movement (opening → sold → received → closing) ──────────────────────

@router.get("/reports/stock-movement")
async def stock_movement_report(
    month: str | None = Query(None, description='Month to report, as "2026-08". Defaults to the current month.'),
    q: str | None = Query(None, description="Filter by product name, colour or size"),
    hide_idle: bool = Query(True, description="Leave out variants with no stock and no movement"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """What happened to each variant's stock over a month.

    Answers the question a buyer actually asks — "we had this much of 1001 Pink
    3XL, we sold this much, and this much new stock is on the way" — which no
    single existing report covered: sales reports ignore stock, and the stock
    report only knows today's figure.

    Opening and closing are reconstructed from the adjustment log rather than
    stored, since nothing snapshots stock at a month boundary: every change
    writes a before/after row, so winding today's figure back through them gives
    the balance on any date.
    """
    from app.models.inventory import InventoryAdjustment
    from app.models.purchase_order import POLineItem, POReceiving, POReceivingItem, PurchaseOrder

    month = month or date.today().strftime("%Y-%m")
    start, end, label = _month_bounds(month)

    def _variant_filter(stmt, name_col, color_col, size_col):
        if not (q and q.strip()):
            return stmt
        for tok in (t for t in q.split() if t.strip()):
            like = f"%{tok.strip()}%"
            stmt = stmt.where(or_(name_col.ilike(like), color_col.ilike(like), size_col.ilike(like)))
        return stmt

    # ── Every variant we might report on, with today's stock ──────────────────
    base = (
        select(
            ProductVariant.id.label("variant_id"),
            Product.name.label("product_name"),
            ProductVariant.color,
            ProductVariant.size,
            ProductVariant.sku,
            func.coalesce(func.sum(InventoryRecord.quantity), 0).label("stock_now"),
        )
        .join(Product, Product.id == ProductVariant.product_id)
        .outerjoin(InventoryRecord, InventoryRecord.variant_id == ProductVariant.id)
        .group_by(ProductVariant.id, Product.name, ProductVariant.color, ProductVariant.size, ProductVariant.sku)
    )
    base = _variant_filter(base, Product.name, ProductVariant.color, ProductVariant.size)
    variants = {
        r.variant_id: {
            "variant_id": str(r.variant_id),
            "product_name": r.product_name,
            "color": r.color or "—",
            "size": r.size or "—",
            "sku": r.sku,
            "stock_now": int(r.stock_now or 0),
        }
        for r in (await db.execute(base)).all()
    }
    if not variants:
        return {"period": {"value": month, "label": label}, "available_months": [], "summary": {}, "rows": []}

    ids = list(variants)

    async def _net_change(since: datetime | None = None, after: datetime | None = None) -> dict:
        """Net stock change recorded by the adjustment log over a window."""
        stmt = (
            select(
                InventoryRecord.variant_id,
                func.coalesce(func.sum(InventoryAdjustment.quantity_after - InventoryAdjustment.quantity_before), 0),
            )
            .join(InventoryRecord, InventoryRecord.id == InventoryAdjustment.inventory_record_id)
            .where(InventoryRecord.variant_id.in_(ids))
            .group_by(InventoryRecord.variant_id)
        )
        if since is not None:
            stmt = stmt.where(InventoryAdjustment.created_at >= since)
        if after is not None:
            stmt = stmt.where(InventoryAdjustment.created_at > after)
        return {vid: int(n or 0) for vid, n in (await db.execute(stmt)).all()}

    changed_since_start = await _net_change(since=start)
    changed_after_end = await _net_change(after=end)

    # ── Sold in the month ─────────────────────────────────────────────────────
    sold = {
        vid: int(n or 0)
        for vid, n in (await db.execute(
            select(OrderItem.variant_id, func.coalesce(func.sum(OrderItem.quantity), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.variant_id.in_(ids))
            .where(Order.created_at.between(start, end))
            .where(Order.status.notin_(["cancelled", "refunded"]))
            .group_by(OrderItem.variant_id)
        )).all()
    }

    # ── Received in the month (goods that actually arrived) ───────────────────
    received = {
        vid: int(n or 0)
        for vid, n in (await db.execute(
            select(POLineItem.product_variant_id, func.coalesce(func.sum(POReceivingItem.qty_received), 0))
            .join(POLineItem, POLineItem.id == POReceivingItem.po_line_item_id)
            .join(POReceiving, POReceiving.id == POReceivingItem.receiving_id)
            .where(POLineItem.product_variant_id.in_(ids))
            .where(POReceiving.created_at.between(start, end))
            .group_by(POLineItem.product_variant_id)
        )).all()
    }

    # ── Still on order — new stock booked but not yet in the building ─────────
    ordered = {
        vid: int(n or 0)
        for vid, n in (await db.execute(
            select(POLineItem.product_variant_id, func.coalesce(func.sum(POLineItem.qty_ordered), 0))
            .join(PurchaseOrder, PurchaseOrder.id == POLineItem.po_id)
            .where(POLineItem.product_variant_id.in_(ids))
            .where(PurchaseOrder.status.notin_(["cancelled", "draft", "received"]))
            .group_by(POLineItem.product_variant_id)
        )).all()
    }
    received_on_open_pos = {
        vid: int(n or 0)
        for vid, n in (await db.execute(
            select(POLineItem.product_variant_id, func.coalesce(func.sum(POReceivingItem.qty_received), 0))
            .join(POLineItem, POLineItem.id == POReceivingItem.po_line_item_id)
            .join(PurchaseOrder, PurchaseOrder.id == POLineItem.po_id)
            .where(POLineItem.product_variant_id.in_(ids))
            .where(PurchaseOrder.status.notin_(["cancelled", "draft", "received"]))
            .group_by(POLineItem.product_variant_id)
        )).all()
    }

    rows: list[dict] = []
    for vid, v in variants.items():
        closing = v["stock_now"] - changed_after_end.get(vid, 0)
        opening = v["stock_now"] - changed_since_start.get(vid, 0)
        s = sold.get(vid, 0)
        r = received.get(vid, 0)
        on_order = max(0, ordered.get(vid, 0) - received_on_open_pos.get(vid, 0))
        # Whatever the month's movement is not explained by selling or receiving:
        # manual corrections, returns to stock, a cancelled order restocking. Shown
        # rather than hidden so opening + received - sold + other always equals
        # closing, and the reader can see the row balances.
        other = closing - opening - r + s

        if hide_idle and not any((opening, closing, s, r, on_order, other)):
            continue
        rows.append({
            **v,
            "opening": opening,
            "sold": s,
            "received": r,
            "other": other,
            "closing": closing,
            "on_order": on_order,
        })

    # Busiest first — what moved is what a buyer wants to look at.
    rows.sort(key=lambda x: (-(x["sold"] + x["received"]), x["product_name"], x["color"], x["size"]))

    today = date.today()
    months: list[dict] = []
    cursor = date(today.year, today.month, 1)
    while cursor >= STORE_LAUNCH:
        months.append({"value": cursor.strftime("%Y-%m"), "label": cursor.strftime("%B %Y")})
        cursor = date(cursor.year - (cursor.month == 1), (cursor.month - 2) % 12 + 1, 1)

    return {
        "period": {"value": month, "label": label},
        "available_months": months,
        "summary": {
            "variants": len(rows),
            "opening": sum(x["opening"] for x in rows),
            "sold": sum(x["sold"] for x in rows),
            "received": sum(x["received"] for x in rows),
            "other": sum(x["other"] for x in rows),
            "closing": sum(x["closing"] for x in rows),
            "on_order": sum(x["on_order"] for x in rows),
        },
        "rows": rows,
    }


# ── Profit & Loss ─────────────────────────────────────────────────────────────

@router.get("/reports/profit-loss")
async def profit_loss_report(
    month: str | None = Query(None, description='Month to report, as "2026-08". Defaults to the current month.'),
    date_from: date | None = Query(None, description="Exact start date (overrides month)"),
    date_to: date | None = Query(None, description="Exact end date (overrides month)"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """What was sold, what it cost us, and what was left.

    Sales tax is left out of revenue on purpose: it is collected on the state's
    behalf and owed straight back, so counting it as income overstates the
    business by exactly the amount it will hand over. This is the same treatment
    QuickBooks applies, which is why the figures here line up with a QuickBooks
    Profit & Loss rather than with the dashboard's billed total.

    Cost of goods uses each variant's current weighted-average cost — nothing
    snapshots cost onto an order line at the time of sale, so a variant whose
    cost has since moved is valued at today's figure. Steady costs make this
    exact; a sharp change makes it an estimate, and the response says which
    lines had no cost on file at all.
    """
    if date_from or date_to:
        start, end = _date_range("custom", date_from, date_to)
        label = f"{start.date().isoformat()} to {end.date().isoformat()}"
        month_value = None
    else:
        month = month or date.today().strftime("%Y-%m")
        start, end, label = _month_bounds(month)
        month_value = month

    sold_ok = Order.status.notin_(["cancelled", "refunded"])

    # ── Order-level money: what was billed, split into its parts ──────────────
    totals = (await db.execute(
        select(
            func.count(Order.id.distinct()),
            func.coalesce(func.sum(Order.subtotal), 0),
            func.coalesce(func.sum(Order.shipping_cost), 0),
            func.coalesce(func.sum(Order.tax_amount), 0),
            func.coalesce(func.sum(Order.discount_amount), 0),
            func.coalesce(func.sum(Order.total), 0),
        ).where(Order.created_at.between(start, end), sold_ok)
    )).one()
    orders, subtotal, shipping, tax, discount, billed = (float(v or 0) for v in totals)
    orders = int(orders)

    # Refunds actually paid back reduce what was earned.
    from app.models.rma import RMARequest
    refunds = float((await db.execute(
        select(func.coalesce(func.sum(RMARequest.refund_amount), 0))
        .select_from(RMARequest)
        .join(Order, RMARequest.order_id == Order.id)
        .where(Order.created_at.between(start, end))
        .where(RMARequest.status == "approved", RMARequest.refund_status == "refunded")
    )).scalar() or 0)

    # ── Cost of goods, per product ────────────────────────────────────────────
    lines = (await db.execute(
        select(
            OrderItem.product_name,
            func.coalesce(func.sum(OrderItem.quantity), 0).label("units"),
            func.coalesce(func.sum(OrderItem.line_total), 0).label("revenue"),
            func.coalesce(
                func.sum(OrderItem.quantity * func.coalesce(ProductVariant.cost_per_item, 0)), 0
            ).label("cogs"),
            func.coalesce(
                func.sum(case((ProductVariant.cost_per_item.is_(None), OrderItem.quantity), else_=0)), 0
            ).label("units_without_cost"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .outerjoin(ProductVariant, ProductVariant.id == OrderItem.variant_id)
        .where(Order.created_at.between(start, end), sold_ok)
        .group_by(OrderItem.product_name)
        .order_by(func.sum(OrderItem.line_total).desc())
    )).all()

    products = []
    cogs_total = 0.0
    units_without_cost = 0
    for r in lines:
        rev = float(r.revenue or 0)
        cogs = float(r.cogs or 0)
        cogs_total += cogs
        units_without_cost += int(r.units_without_cost or 0)
        products.append({
            "product_name": r.product_name or "—",
            "units": int(r.units or 0),
            "revenue": round(rev, 2),
            "cogs": round(cogs, 2),
            "gross_profit": round(rev - cogs, 2),
            "margin_pct": round((rev - cogs) / rev * 100, 1) if rev else None,
            "units_without_cost": int(r.units_without_cost or 0),
        })

    # Revenue excludes the tax and is net of refunds — the figure a P&L reports.
    revenue = billed - tax - refunds
    gross_profit = revenue - cogs_total

    today = date.today()
    months: list[dict] = []
    cursor = date(today.year, today.month, 1)
    while cursor >= STORE_LAUNCH:
        months.append({"value": cursor.strftime("%Y-%m"), "label": cursor.strftime("%B %Y")})
        cursor = date(cursor.year - (cursor.month == 1), (cursor.month - 2) % 12 + 1, 1)

    return {
        "period": {"value": month_value, "label": label,
                   "from": start.date().isoformat(), "to": end.date().isoformat()},
        "available_months": months,
        "summary": {
            "orders": orders,
            "product_sales": round(subtotal - discount, 2),
            "shipping_charged": round(shipping, 2),
            "discounts": round(discount, 2),
            "sales_tax_excluded": round(tax, 2),
            "refunds": round(refunds, 2),
            "revenue": round(revenue, 2),
            "cogs": round(cogs_total, 2),
            "gross_profit": round(gross_profit, 2),
            "margin_pct": round(gross_profit / revenue * 100, 1) if revenue else None,
            "units_without_cost": units_without_cost,
        },
        "products": products,
    }


# ── Commission owed to tiered customers ───────────────────────────────────────

#: Which tiers earn commission, and at what rate. Held here rather than spread
#: through the query so the arrangement can be read — and changed — in one place
#: when the client renegotiates it.
COMMISSION_TIERS = ("Tier 4", "Tier 5")


def _tier_key(name: str | None) -> str:
    """A tier name reduced to what it actually says.

    "Tier 4", "TIER 4", "tier-4" and "Tier4" are one tier written four ways, and
    which of them is in the database is not worth a report quietly showing
    nothing. Case, spaces and dashes are dropped before comparing.
    """
    return "".join(ch for ch in (name or "").upper() if ch.isalnum())
#: Codes 1000 and 1001 are the value tee, sold at a thinner margin, so they earn
#: less. Everything else earns the standard rate.
COMMISSION_SPECIAL_CODES = ("1000", "1001")
COMMISSION_SPECIAL_PERCENT = 10.0
COMMISSION_DEFAULT_PERCENT = 18.0


def _product_code_of(product_code: str | None, product_name: str | None) -> str:
    """The catalogue number for a sold line.

    Normally read straight off the product. An order keeps its own copy of the
    name — "1000 Blended Unisex Tee" — which still carries the number at the
    front, and that is what answers for a line whose product has since been
    deleted or renamed. Guessing wrong here would put a line in the wrong
    commission band, so it falls back to the name only when there is nothing
    better.
    """
    code = (product_code or "").strip()
    if code:
        return code
    lead = (product_name or "").strip().split(" ", 1)[0]
    return lead if lead.isdigit() else ""


@router.get("/reports/commission")
async def commission_report(
    date_from: date | None = Query(None, description="Start date (inclusive)"),
    date_to: date | None = Query(None, description="End date (inclusive)"),
    period: str = Query("month", description="Rolling period, used when no dates are given"),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """What each tiered customer has earned in commission, and on what.

    Commission is worked out on what the customer was actually billed for the
    goods — the line price on their order — because those are the same figures
    as the agreed rate card. Shipping, tax and the convenience fee are not goods
    and earn nothing.

    Only settled orders count. An order that has not been paid for has not
    earned anyone anything yet, and a refunded one has un-earned it.
    """
    start, end = _date_range(period, date_from, date_to)

    from app.models.pricing import PricingTier

    # Which tiers these are is resolved first, by name rather than by id, so a
    # tier renamed or recreated still earns.
    _wanted = {_tier_key(t) for t in COMMISSION_TIERS}
    _tier_ids = [
        tid for tid, tname in (await db.execute(select(PricingTier.id, PricingTier.name))).all()
        if _tier_key(tname) in _wanted
    ]
    if not _tier_ids:
        return {
            "period": {"from": start.date().isoformat(), "to": end.date().isoformat()},
            "rules": {
                "tiers": list(COMMISSION_TIERS),
                "special_codes": list(COMMISSION_SPECIAL_CODES),
                "special_percent": COMMISSION_SPECIAL_PERCENT,
                "default_percent": COMMISSION_DEFAULT_PERCENT,
            },
            "totals": {"customers": 0, "special_base": 0.0, "special_commission": 0.0,
                       "other_base": 0.0, "other_commission": 0.0, "total_commission": 0.0},
            "customers": [],
            # Said out loud rather than shown as an empty table: no matching tier
            # is a setup problem, not a quiet month.
            "warning": (
                f"No pricing tier is named {' or '.join(COMMISSION_TIERS)}. "
                "Check the tier names under Pricing Tiers — nothing can earn "
                "commission until one of them matches."
            ),
        }

    rows = (await db.execute(
        select(
            Company.id.label("company_id"),
            Company.name.label("company_name"),
            PricingTier.name.label("tier_name"),
            Order.id.label("order_id"),
            Order.order_number,
            Order.created_at,
            Product.product_code,
            OrderItem.product_name,
            OrderItem.quantity,
            OrderItem.line_total,
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Company, Company.id == Order.company_id)
        .join(PricingTier, PricingTier.id == Company.pricing_tier_id)
        .outerjoin(ProductVariant, ProductVariant.id == OrderItem.variant_id)
        .outerjoin(Product, Product.id == ProductVariant.product_id)
        .where(
            Company.pricing_tier_id.in_(_tier_ids),
            Order.payment_status == "paid",
            Order.status.notin_(["cancelled", "refunded"]),
            Order.created_at.between(start, end),
        )
        .order_by(Company.name.asc(), Order.created_at.asc())
    )).mappings().all()

    # Built up per customer, and per order inside that, so the total on screen
    # can be opened up and checked against the orders it came from.
    customers: dict = {}
    for r in rows:
        cid = str(r["company_id"])
        cust = customers.setdefault(cid, {
            "company_id": cid,
            "company_name": r["company_name"],
            "tier": r["tier_name"],
            "special_base": 0.0, "special_commission": 0.0,
            "other_base": 0.0, "other_commission": 0.0,
            "total_commission": 0.0,
            "_orders": {},
        })

        code = _product_code_of(r["product_code"], r["product_name"])
        is_special = code in COMMISSION_SPECIAL_CODES
        base = float(r["line_total"] or 0)
        rate = COMMISSION_SPECIAL_PERCENT if is_special else COMMISSION_DEFAULT_PERCENT
        earned = round(base * rate / 100, 2)

        if is_special:
            cust["special_base"] += base
            cust["special_commission"] += earned
        else:
            cust["other_base"] += base
            cust["other_commission"] += earned
        cust["total_commission"] += earned

        o = cust["_orders"].setdefault(str(r["order_id"]), {
            "order_id": str(r["order_id"]),
            "order_number": r["order_number"],
            "date": r["created_at"].isoformat() if r["created_at"] else None,
            "special_base": 0.0, "special_commission": 0.0,
            "other_base": 0.0, "other_commission": 0.0,
            "total_commission": 0.0,
            "units": 0,
        })
        o["units"] += int(r["quantity"] or 0)
        if is_special:
            o["special_base"] += base
            o["special_commission"] += earned
        else:
            o["other_base"] += base
            o["other_commission"] += earned
        o["total_commission"] += earned

    out = []
    for c in customers.values():
        orders = sorted(c.pop("_orders").values(), key=lambda o: o["date"] or "")
        for o in orders:
            for k in ("special_base", "special_commission", "other_base",
                      "other_commission", "total_commission"):
                o[k] = round(o[k], 2)
        for k in ("special_base", "special_commission", "other_base",
                  "other_commission", "total_commission"):
            c[k] = round(c[k], 2)
        c["order_count"] = len(orders)
        c["orders"] = orders
        out.append(c)

    out.sort(key=lambda c: c["total_commission"], reverse=True)

    return {
        "period": {"from": start.date().isoformat(), "to": end.date().isoformat()},
        "rules": {
            "tiers": list(COMMISSION_TIERS),
            "special_codes": list(COMMISSION_SPECIAL_CODES),
            "special_percent": COMMISSION_SPECIAL_PERCENT,
            "default_percent": COMMISSION_DEFAULT_PERCENT,
        },
        "totals": {
            "customers": len(out),
            "special_base": round(sum(c["special_base"] for c in out), 2),
            "special_commission": round(sum(c["special_commission"] for c in out), 2),
            "other_base": round(sum(c["other_base"] for c in out), 2),
            "other_commission": round(sum(c["other_commission"] for c in out), 2),
            "total_commission": round(sum(c["total_commission"] for c in out), 2),
        },
        "customers": out,
    }
