"""Admin — Pages SEO CRUD."""
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.base import Base

router = APIRouter(prefix="/admin/pages-seo")

PREDEFINED_PAGES = [
    {"slug": "home", "name": "Home"},
    {"slug": "about", "name": "About Us"},
    {"slug": "contact", "name": "Contact Us"},
    {"slug": "private-label", "name": "Private Label"},
    {"slug": "print-guide", "name": "Print Guide"},
    {"slug": "privacy-policy", "name": "Privacy Policy"},
    {"slug": "style-sheets", "name": "Style Sheets"},
    {"slug": "product-specs", "name": "Product Specs"},
    {"slug": "blog", "name": "Blog"},
]


class PageSeo(Base):
    __tablename__ = "page_seo"

    id: Mapped[str] = mapped_column(primary_key=True, server_default="gen_random_uuid()")
    page_slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    meta_title: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    meta_description: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    og_image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(server_default="now()", onupdate="now()")


class PageSeoUpdate(BaseModel):
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    keywords: Optional[str] = None
    og_image_url: Optional[str] = None


def _row(p: PageSeo, name: str = "") -> dict:
    return {
        "page_slug": p.page_slug,
        "page_name": name,
        "meta_title": p.meta_title,
        "meta_description": p.meta_description,
        "keywords": p.keywords,
        "og_image_url": p.og_image_url,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("")
async def list_pages_seo(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(PageSeo))).scalars().all()
    existing = {r.page_slug: r for r in rows}
    result = []
    for pg in PREDEFINED_PAGES:
        slug = pg["slug"]
        if slug in existing:
            result.append(_row(existing[slug], pg["name"]))
        else:
            result.append({
                "page_slug": slug,
                "page_name": pg["name"],
                "meta_title": None,
                "meta_description": None,
                "keywords": None,
                "og_image_url": None,
                "updated_at": None,
            })
    return result


@router.get("/{slug}")
async def get_page_seo(
    slug: str,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(PageSeo).where(PageSeo.page_slug == slug))).scalar_one_or_none()
    name = next((p["name"] for p in PREDEFINED_PAGES if p["slug"] == slug), slug)
    if not row:
        return {
            "page_slug": slug,
            "page_name": name,
            "meta_title": None,
            "meta_description": None,
            "keywords": None,
            "og_image_url": None,
            "updated_at": None,
        }
    return _row(row, name)


@router.patch("/{slug}")
async def upsert_page_seo(
    slug: str,
    payload: PageSeoUpdate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(select(PageSeo).where(PageSeo.page_slug == slug))).scalar_one_or_none()
    if not row:
        row = PageSeo(page_slug=slug)
        db.add(row)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    name = next((p["name"] for p in PREDEFINED_PAGES if p["slug"] == slug), slug)
    return _row(row, name)
