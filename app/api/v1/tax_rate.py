"""Public — look up tax rate by state/region."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

router = APIRouter(prefix="/tax-rate")


@router.get("")
async def get_tax_rate(region: str, db: AsyncSession = Depends(get_db)):
    from app.api.v1.admin.taxes import TaxRate
    r = (await db.execute(
        select(TaxRate).where(
            TaxRate.region == region.upper(),
            TaxRate.is_enabled == True,  # noqa: E712
        )
    )).scalar_one_or_none()
    if r:
        return {"region": r.region, "rate": float(r.rate), "name": r.name}
    return {"region": region.upper(), "rate": 0.0, "name": None}
