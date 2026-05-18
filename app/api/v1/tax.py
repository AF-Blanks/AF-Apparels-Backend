"""POST /api/v1/tax/calculate — ZipTax-backed tax calculation."""
import logging
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tax")


class TaxCalculateRequest(BaseModel):
    subtotal: float
    zip_code: str
    state: str
    discount: float = 0.0


@router.post("/calculate")
async def calculate_tax(
    body: TaxCalculateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    state = body.state.upper()
    taxable_subtotal = max(0.0, body.subtotal - body.discount)

    # Tax-exempt companies pay no tax
    company_id = getattr(request.state, "company_id", None)
    if company_id:
        from app.models.company import Company
        company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
        if company and company.tax_exempt:
            return {"tax_rate": 0.0, "tax_amount": 0.0, "region": state, "taxable": False, "source": "exempt"}

    # ZipTax: use when API key is configured and we have address data
    if body.zip_code and taxable_subtotal > 0:
        from app.services.tax_service import calculate_tax as ziptax_calc, get_ziptax_client
        if get_ziptax_client() is not None:
            result = await ziptax_calc(
                to_state=state,
                to_zip=body.zip_code,
                to_city="",
                subtotal=taxable_subtotal,
                shipping=0,
            )
            if result.get("source") == "ziptax":
                logger.info(
                    "ZipTax: %s %s → rate=%.4f%% amount=$%.2f",
                    state, body.zip_code, result["rate"], result["tax_amount"],
                )
                return {
                    "tax_rate": result["rate"],
                    "tax_amount": result["tax_amount"],
                    "region": result["region"],
                    "taxable": result["tax_amount"] > 0,
                    "source": "ziptax",
                }

    # Fallback: manual tax_rates table
    from app.api.v1.admin.taxes import TaxRate
    r = (await db.execute(
        select(TaxRate).where(TaxRate.region == state, TaxRate.is_enabled == True)  # noqa: E712
    )).scalar_one_or_none()

    if r:
        rate = float(r.rate)
        tax_amount = round(taxable_subtotal * rate / 100, 2)
        return {
            "tax_rate": rate,
            "tax_amount": tax_amount,
            "region": r.region,
            "taxable": tax_amount > 0,
            "source": "manual",
        }

    return {"tax_rate": 0.0, "tax_amount": 0.0, "region": state, "taxable": False, "source": "none"}
