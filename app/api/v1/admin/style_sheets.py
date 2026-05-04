"""Admin — style sheets CRUD."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.base import BaseModel as DBBaseModel

router = APIRouter(prefix="/admin/style-sheets")


class StyleSheet(DBBaseModel):
    __tablename__ = "style_sheets"

    style_number: Mapped[str] = mapped_column(String(50), nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class StyleSheetCreate(BaseModel):
    style_number: str
    image_url: Optional[str] = None
    pdf_url: Optional[str] = None
    sort_order: int = 0
    is_active: bool = True


class StyleSheetUpdate(BaseModel):
    style_number: Optional[str] = None
    image_url: Optional[str] = None
    pdf_url: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


def _row(s: StyleSheet) -> dict:
    return {
        "id": str(s.id),
        "style_number": s.style_number,
        "image_url": s.image_url,
        "pdf_url": s.pdf_url,
        "sort_order": s.sort_order,
        "is_active": s.is_active,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get("")
async def list_style_sheets(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        rows = (await db.execute(select(StyleSheet).order_by(StyleSheet.sort_order, StyleSheet.style_number))).scalars().all()
        return [_row(s) for s in rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"style_sheets query failed: {exc}")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_style_sheet(
    payload: StyleSheetCreate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = StyleSheet(**payload.model_dump())
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _row(s)


@router.patch("/{sheet_id}")
async def update_style_sheet(
    sheet_id: uuid.UUID,
    payload: StyleSheetUpdate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = (await db.execute(select(StyleSheet).where(StyleSheet.id == sheet_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Style sheet not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(s, field, value)
    await db.commit()
    await db.refresh(s)
    return _row(s)


@router.delete("/{sheet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_style_sheet(
    sheet_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = (await db.execute(select(StyleSheet).where(StyleSheet.id == sheet_id))).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Style sheet not found")
    await db.delete(s)
    await db.commit()
