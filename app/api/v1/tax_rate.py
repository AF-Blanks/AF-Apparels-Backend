"""Public — calculate sales tax via TaxJar (fallback: manual tax_rates table)."""
import logging
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tax-rate")


@router.get("")
async def get_tax_rate(
    request: Request,
    region: str = Query(..., description="Two-letter US state code"),
    zip_code: str = Query("", description="Shipping ZIP code for zip-level accuracy"),
    city: str = Query("", description="Shipping city"),
    subtotal: float = Query(0.0, description="Cart subtotal for TaxJar calculation"),
    shipping: float = Query(0.0, description="Shipping cost for TaxJar calculation"),
    db: AsyncSession = Depends(get_db),
):
    state = region.upper()

    # ── Tax-exempt companies pay no tax ──────────────────────────────────────
    company_id = getattr(request.state, "company_id", None)
    if company_id:
        from app.models.company import Company
        company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
        if company and company.tax_exempt:
            return {"rate": 0.0, "tax_amount": 0.0, "region": state, "source": "exempt"}

    # ── TaxJar: use when API key is configured and we have enough address data ──
    if zip_code and subtotal > 0:
        from app.services.taxjar_service import calculate_tax, get_taxjar_client
        if get_taxjar_client() is not None:
            result = await calculate_tax(
                to_state=state,
                to_zip=zip_code,
                to_city=city,
                subtotal=subtotal,
                shipping=shipping,
            )
            if result.get("source") == "taxjar":
                logger.info(
                    "TaxJar: %s %s → rate=%.4f%% amount=$%.2f",
                    state, zip_code, result["rate"], result["tax_amount"],
                )
                return result

    # ── Fallback: manual tax_rates table ──────────────────────────────────────
    from app.api.v1.admin.taxes import TaxRate
    r = (await db.execute(
        select(TaxRate).where(
            TaxRate.region == state,
            TaxRate.is_enabled == True,  # noqa: E712
        )
    )).scalar_one_or_none()

    if r:
        rate = float(r.rate)
        return {
            "rate": rate,
            "tax_amount": round(subtotal * rate / 100, 2),
            "region": r.region,
            "source": "manual",
        }

    return {"rate": 0.0, "tax_amount": 0.0, "region": state, "source": "none"}
