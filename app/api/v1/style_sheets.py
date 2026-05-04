"""Public — style sheets listing."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.v1.admin.style_sheets import StyleSheet, _row

router = APIRouter(prefix="/style-sheets")


@router.get("")
async def list_style_sheets_public(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(StyleSheet)
            .where(StyleSheet.is_active == True)  # noqa: E712
            .order_by(StyleSheet.sort_order, StyleSheet.style_number)
        )
    ).scalars().all()
    return [_row(s) for s in rows]
