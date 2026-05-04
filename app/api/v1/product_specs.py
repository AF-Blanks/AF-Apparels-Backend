"""Public — product specs listing."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.v1.admin.product_specs import ProductSpec, _row

router = APIRouter(prefix="/product-specs")


@router.get("")
async def list_product_specs_public(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ProductSpec)
            .where(ProductSpec.is_active == True)  # noqa: E712
            .order_by(ProductSpec.sort_order, ProductSpec.title)
        )
    ).scalars().all()
    return [_row(p) for p in rows]
