"""Admin — tax rates CRUD."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Boolean, Numeric, String, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.base import BaseModel as DBBaseModel

router = APIRouter(prefix="/admin/taxes")


# ── Inline model (avoids separate file for a small feature) ──────────────────

class TaxRate(DBBaseModel):
    __tablename__ = "tax_rates"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    region: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    applies_to: Mapped[str] = mapped_column(
        String(20), default="all", nullable=False,
        comment="all | retail_only | wholesale_only"
    )


# ── Schemas ───────────────────────────────────────────────────────────────────

class TaxRateCreate(BaseModel):
    name: str
    region: str
    rate: float
    is_enabled: bool = True
    applies_to: str = "all"


class TaxRateUpdate(BaseModel):
    name: Optional[str] = None
    region: Optional[str] = None
    rate: Optional[float] = None
    is_enabled: Optional[bool] = None
    applies_to: Optional[str] = None


def _row(t: TaxRate) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "region": t.region,
        "rate": float(t.rate),
        "is_enabled": t.is_enabled,
        "applies_to": t.applies_to,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
async def list_tax_rates(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(TaxRate).order_by(TaxRate.region, TaxRate.name))).scalars().all()
    return [_row(t) for t in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_tax_rate(
    payload: TaxRateCreate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    t = TaxRate(**payload.model_dump())
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.patch("/{tax_id}")
async def update_tax_rate(
    tax_id: uuid.UUID,
    payload: TaxRateUpdate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    t = (await db.execute(select(TaxRate).where(TaxRate.id == tax_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(t, field, value)
    await db.commit()
    await db.refresh(t)
    return _row(t)


@router.delete("/{tax_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tax_rate(
    tax_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    t = (await db.execute(select(TaxRate).where(TaxRate.id == tax_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Tax rate not found")
    await db.delete(t)
    await db.commit()
