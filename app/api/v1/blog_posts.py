"""Public — blog posts listing and detail."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.v1.admin.blog_posts import BlogPost, _row

router = APIRouter(prefix="/blog-posts")


@router.get("")
async def list_blog_posts_public(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(BlogPost)
            .where(BlogPost.status == "published")
            .order_by(BlogPost.published_date.desc().nullslast(), BlogPost.created_at.desc())
        )
    ).scalars().all()
    return [_row(p) for p in rows]


@router.get("/{slug}")
async def get_blog_post_public(slug: str, db: AsyncSession = Depends(get_db)):
    post = (
        await db.execute(
            select(BlogPost).where(BlogPost.slug == slug, BlogPost.status == "published")
        )
    ).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Blog post not found")
    return _row(post)
