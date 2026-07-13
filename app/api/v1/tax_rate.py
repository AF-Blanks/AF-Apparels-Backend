"""Public — tax rate endpoint. Tax is now handled by QuickBooks AST on invoice."""
import logging
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tax-rate")


@router.get("")
async def get_tax_rate(
    request: Request,
    region: str = "",
    zip_code: str = "",
    city: str = "",
    subtotal: float = 0.0,
    shipping: float = 0.0,
    discount: float = 0.0,
    db: AsyncSession = Depends(get_db),
):
    """Tax calculation disabled at checkout — QB Automated Sales Tax handles tax on invoice."""
    state = region.upper() if region else ""
    logger.info("Tax rate endpoint called — QB AST handles tax on invoice (state=%s zip=%s)", state, zip_code)
    return {"rate": 0.0, "tax_amount": 0.0, "region": state, "source": "qb_ast"}
