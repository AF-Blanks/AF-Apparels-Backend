"""Public — page SEO metadata."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.v1.admin.pages_seo import PageSeo

router = APIRouter(prefix="/pages-seo")


@router.get("/{slug}")
async def get_page_seo_public(slug: str, db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(PageSeo).where(PageSeo.page_slug == slug))).scalar_one_or_none()
    if not row:
        return {"page_slug": slug, "meta_title": None, "meta_description": None, "keywords": None, "og_image_url": None}
    return {
        "page_slug": row.page_slug,
        "meta_title": row.meta_title,
        "meta_description": row.meta_description,
        "keywords": row.keywords,
        "og_image_url": row.og_image_url,
    }
