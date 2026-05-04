"""Admin — product specs CRUD."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Boolean, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.base import BaseModel as DBBaseModel

router = APIRouter(prefix="/admin/product-specs")


class ProductSpec(DBBaseModel):
    __tablename__ = "product_specs"

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pdf_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ProductSpecCreate(BaseModel):
    title: str
    description: Optional[str] = None
    pdf_url: Optional[str] = None
    sort_order: int = 0
    is_active: bool = True


class ProductSpecUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    pdf_url: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


def _row(p: ProductSpec) -> dict:
    return {
        "id": str(p.id),
        "title": p.title,
        "description": p.description,
        "pdf_url": p.pdf_url,
        "sort_order": p.sort_order,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("")
async def list_product_specs(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(ProductSpec).order_by(ProductSpec.sort_order, ProductSpec.title))).scalars().all()
    return [_row(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product_spec(
    payload: ProductSpecCreate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    p = ProductSpec(**payload.model_dump())
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _row(p)


@router.put("/{spec_id}")
async def update_product_spec(
    spec_id: uuid.UUID,
    payload: ProductSpecUpdate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    p = (await db.execute(select(ProductSpec).where(ProductSpec.id == spec_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Product spec not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(p, field, value)
    await db.commit()
    await db.refresh(p)
    return _row(p)


@router.delete("/{spec_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product_spec(
    spec_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    p = (await db.execute(select(ProductSpec).where(ProductSpec.id == spec_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Product spec not found")
    await db.delete(p)
    await db.commit()
