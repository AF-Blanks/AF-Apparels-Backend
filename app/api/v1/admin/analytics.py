"""Admin analytics endpoint."""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.company import Company
from app.models.order import Order, OrderItem
from app.models.product import Product, ProductVariant
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin-analytics"])

# Statuses that count as real revenue
ACTIVE_STATUSES = ("pending", "confirmed", "processing", "ready_for_pickup", "shipped", "delivered")


def _date_range(period: str, start_date: Optional[str], end_date: Optional[str]):
    """Return (current_start, current_end, prev_start, prev_end) as date objects."""
    today = date.today()
    if period == "today":
        cur_start = cur_end = today
    elif period == "7d":
        cur_start = today - timedelta(days=6)
        cur_end = today
    elif period == "90d":
        cur_start = today - timedelta(days=89)
        cur_end = today
    elif period == "custom" and start_date and end_date:
        cur_start = date.fromisoformat(start_date)
        cur_end = date.fromisoformat(end_date)
    else:  # default 30d
        cur_start = today - timedelta(days=29)
        cur_end = today

    span = (cur_end - cur_start).days + 1
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    return cur_start, cur_end, prev_start, prev_end


def _pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round((current - previous) / previous * 100, 1)


@router.get("/analytics")
async def get_analytics(
    period: str = Query("30d", pattern="^(today|7d|30d|90d|custom)$"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    cur_start, cur_end, prev_start, prev_end = _date_range(period, start_date, end_date)

    cur_start_dt = datetime(cur_start.year, cur_start.month, cur_start.day, tzinfo=timezone.utc)
    cur_end_dt = datetime(cur_end.year, cur_end.month, cur_end.day, 23, 59, 59, tzinfo=timezone.utc)
    prev_start_dt = datetime(prev_start.year, prev_start.month, prev_start.day, tzinfo=timezone.utc)
    prev_end_dt = datetime(prev_end.year, prev_end.month, prev_end.day, 23, 59, 59, tzinfo=timezone.utc)

    # ── Current period overview ───────────────────────────────────────────────
    cur_q = await db.execute(
        select(
            func.count(Order.id).label("total_orders"),
            func.coalesce(func.sum(Order.total), 0).label("total_revenue"),
            func.count(Order.company_id.distinct()).label("wholesale_companies"),
            func.sum(case((Order.is_guest_order == True, 1), else_=0)).label("guest_orders"),
            func.sum(case((Order.is_guest_order == False, 1), else_=0)).label("wholesale_orders"),
        ).where(
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
            Order.status.in_(ACTIVE_STATUSES),
        )
    )
    cur_row = cur_q.one()

    # ── Previous period overview ──────────────────────────────────────────────
    prev_q = await db.execute(
        select(
            func.count(Order.id).label("total_orders"),
            func.coalesce(func.sum(Order.total), 0).label("total_revenue"),
        ).where(
            Order.created_at >= prev_start_dt,
            Order.created_at <= prev_end_dt,
            Order.status.in_(ACTIVE_STATUSES),
        )
    )
    prev_row = prev_q.one()

    cur_revenue = float(cur_row.total_revenue or 0)
    cur_orders = int(cur_row.total_orders or 0)
    prev_revenue = float(prev_row.total_revenue or 0)
    prev_orders = int(prev_row.total_orders or 0)
    aov = cur_revenue / cur_orders if cur_orders else 0

    # Paid orders for conversion rate
    paid_q = await db.execute(
        select(func.count(Order.id)).where(
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
            Order.payment_status == "paid",
        )
    )
    paid_orders = int(paid_q.scalar() or 0)
    total_orders_all = await db.scalar(
        select(func.count(Order.id)).where(
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
        )
    )
    conversion_rate = round(paid_orders / total_orders_all * 100, 1) if total_orders_all else 0.0

    # ── Customer counts ───────────────────────────────────────────────────────
    # Total unique companies that ever ordered
    total_cust_q = await db.execute(
        select(func.count(Company.id.distinct())).where(Company.status == "active")
    )
    total_customers = int(total_cust_q.scalar() or 0)

    # New customers in current period: companies with first order in period
    new_cust_q = await db.execute(
        select(func.count()).select_from(
            select(Order.company_id)
            .where(
                Order.company_id.isnot(None),
                Order.status.in_(ACTIVE_STATUSES),
            )
            .group_by(Order.company_id)
            .having(func.min(Order.created_at) >= cur_start_dt)
            .having(func.min(Order.created_at) <= cur_end_dt)
            .subquery()
        )
    )
    new_customers = int(new_cust_q.scalar() or 0)

    # Returning = ordered in current period but not new
    ret_cust_q = await db.execute(
        select(func.count(Order.company_id.distinct())).where(
            Order.company_id.isnot(None),
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
            Order.status.in_(ACTIVE_STATUSES),
        )
    )
    ordered_this_period = int(ret_cust_q.scalar() or 0)
    returning_customers = max(0, ordered_this_period - new_customers)

    # Prev period customers (unique companies)
    prev_cust_q = await db.execute(
        select(func.count(Order.company_id.distinct())).where(
            Order.company_id.isnot(None),
            Order.created_at >= prev_start_dt,
            Order.created_at <= prev_end_dt,
            Order.status.in_(ACTIVE_STATUSES),
        )
    )
    prev_customers = int(prev_cust_q.scalar() or 0)

    # ── Revenue chart (daily) ─────────────────────────────────────────────────
    chart_q = await db.execute(
        select(
            func.date_trunc("day", Order.created_at).label("day"),
            func.coalesce(func.sum(Order.total), 0).label("revenue"),
            func.count(Order.id).label("orders"),
        ).where(
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
            Order.status.in_(ACTIVE_STATUSES),
        ).group_by(text("1")).order_by(text("1"))
    )
    chart_rows = chart_q.all()

    # Fill missing days with 0
    date_map: dict[str, dict] = {}
    d = cur_start
    while d <= cur_end:
        date_map[d.isoformat()] = {"date": d.isoformat(), "revenue": 0.0, "orders": 0}
        d += timedelta(days=1)
    for row in chart_rows:
        day_str = row.day.date().isoformat()
        if day_str in date_map:
            date_map[day_str]["revenue"] = round(float(row.revenue), 2)
            date_map[day_str]["orders"] = int(row.orders)
    revenue_chart = list(date_map.values())

    # ── Order status breakdown ────────────────────────────────────────────────
    status_q = await db.execute(
        select(
            Order.status,
            func.count(Order.id).label("count"),
            func.coalesce(func.sum(Order.total), 0).label("revenue"),
        ).where(
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
        ).group_by(Order.status)
    )
    order_status_breakdown = [
        {"status": r.status, "count": int(r.count), "revenue": round(float(r.revenue), 2)}
        for r in status_q.all()
    ]

    # ── Top products ──────────────────────────────────────────────────────────
    top_prod_q = await db.execute(
        select(
            OrderItem.product_name,
            func.sum(OrderItem.quantity).label("units_sold"),
            func.sum(OrderItem.line_total).label("revenue"),
            ProductVariant.product_id,
        ).join(
            Order, OrderItem.order_id == Order.id
        ).join(
            ProductVariant, OrderItem.variant_id == ProductVariant.id
        ).where(
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
            Order.status.in_(ACTIVE_STATUSES),
        ).group_by(OrderItem.product_name, ProductVariant.product_id)
        .order_by(func.sum(OrderItem.line_total).desc())
        .limit(10)
    )
    top_prod_rows = top_prod_q.all()

    # Fetch slugs for the product IDs
    prod_ids = [r.product_id for r in top_prod_rows]
    slug_map: dict = {}
    if prod_ids:
        slug_q = await db.execute(
            select(Product.id, Product.slug).where(Product.id.in_(prod_ids))
        )
        slug_map = {str(r.id): r.slug for r in slug_q.all()}

    top_products = [
        {
            "product_name": r.product_name,
            "units_sold": int(r.units_sold),
            "revenue": round(float(r.revenue), 2),
            "slug": slug_map.get(str(r.product_id), ""),
        }
        for r in top_prod_rows
    ]

    # ── Top customers ─────────────────────────────────────────────────────────
    top_cust_q = await db.execute(
        select(
            Company.name.label("company_name"),
            Company.id.label("company_id"),
            func.count(Order.id).label("orders"),
            func.sum(Order.total).label("total_spend"),
        ).join(
            Order, Order.company_id == Company.id
        ).where(
            Order.created_at >= cur_start_dt,
            Order.created_at <= cur_end_dt,
            Order.status.in_(ACTIVE_STATUSES),
        ).group_by(Company.id, Company.name)
        .order_by(func.sum(Order.total).desc())
        .limit(10)
    )
    top_customers = [
        {
            "company_name": r.company_name,
            "company_id": str(r.company_id),
            "orders": int(r.orders),
            "total_spend": round(float(r.total_spend or 0), 2),
        }
        for r in top_cust_q.all()
    ]

    # ── Orders by state ───────────────────────────────────────────────────────
    # Extract state from shipping_address_snapshot JSON
    state_q = await db.execute(
        text("""
            SELECT
                shipping_address_snapshot::json->>'state' AS state,
                COUNT(*) AS orders,
                COALESCE(SUM(total), 0) AS revenue
            FROM orders
            WHERE created_at >= :start
              AND created_at <= :end
              AND status = ANY(:statuses)
              AND shipping_address_snapshot IS NOT NULL
              AND shipping_address_snapshot::json->>'state' IS NOT NULL
              AND shipping_address_snapshot::json->>'state' <> ''
            GROUP BY 1
            ORDER BY orders DESC
            LIMIT 10
        """),
        {"start": cur_start_dt, "end": cur_end_dt, "statuses": list(ACTIVE_STATUSES)},
    )
    orders_by_state = [
        {"state": r.state, "orders": int(r.orders), "revenue": round(float(r.revenue), 2)}
        for r in state_q.all()
    ]

    return {
        "overview": {
            "total_revenue": round(cur_revenue, 2),
            "total_orders": cur_orders,
            "average_order_value": round(aov, 2),
            "total_customers": total_customers,
            "new_customers": new_customers,
            "returning_customers": returning_customers,
            "guest_orders": int(cur_row.guest_orders or 0),
            "wholesale_orders": int(cur_row.wholesale_orders or 0),
            "revenue_change_percent": _pct_change(cur_revenue, prev_revenue),
            "orders_change_percent": _pct_change(cur_orders, prev_orders),
            "customers_change_percent": _pct_change(ordered_this_period, prev_customers),
            "conversion_rate": conversion_rate,
        },
        "revenue_chart": revenue_chart,
        "order_status_breakdown": order_status_breakdown,
        "top_products": top_products,
        "top_customers": top_customers,
        "orders_by_state": orders_by_state,
        "new_vs_returning": {
            "new": new_customers,
            "returning": returning_customers,
        },
    }
