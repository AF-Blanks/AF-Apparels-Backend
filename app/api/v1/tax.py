"""POST /api/v1/tax/calculate — Tax disabled at checkout; handled by QuickBooks AST on invoice."""
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
    """Tax calculation disabled — QB Automated Sales Tax handles tax on invoice."""
    state = body.state.upper() if body.state else ""

    # Tax-exempt companies still get exempt response
    company_id = getattr(request.state, "company_id", None)
    if company_id:
        from app.models.company import Company
        company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
        if company and company.tax_exempt:
            logger.info("Tax: company %s is tax-exempt", company_id)
            return {"tax_rate": 0.0, "tax_amount": 0.0, "region": state, "taxable": False, "source": "exempt"}

    logger.info("Tax calculate: QB AST handles tax on invoice (state=%s zip=%s)", state, body.zip_code)
    return {"tax_rate": 0.0, "tax_amount": 0.0, "region": state, "taxable": False, "source": "qb_ast"}


@router.get("/outbound-ip")
async def outbound_ip():
    """Debug endpoint — returns this service's outbound IP."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            ip = (await client.get("https://ifconfig.me/ip")).text.strip()
        return {"outbound_ip": ip}
    except Exception as exc:
        return {"error": str(exc)}
