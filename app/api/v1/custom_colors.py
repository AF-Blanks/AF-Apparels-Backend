"""Public — custom color name → hex overrides (admin-set colors not in the app's hardcoded palette).

Read by both the admin swatch pickers and the customer-facing product page,
so a color an admin defines once (e.g. "Ferrari Red") resolves correctly
everywhere instead of only on the browser that set it.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.custom_color import CustomSwatchColor

router = APIRouter(prefix="/custom-colors", tags=["custom-colors"])


@router.get("")
async def list_custom_colors(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    result = await db.execute(select(CustomSwatchColor))
    return {row.name: row.hex for row in result.scalars().all()}
