"""Admin — Blog Posts CRUD."""
import uuid
from typing import Optional, Any
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import String, Text, Date, ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.base import Base

router = APIRouter(prefix="/admin/blog-posts")


class BlogPost(Base):
    __tablename__ = "blog_posts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    cover_image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    published_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    read_time: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    excerpt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    article_body: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    faq: Mapped[Optional[Any]] = mapped_column(JSONB, nullable=True)
    tags: Mapped[Optional[list]] = mapped_column(ARRAY(Text), nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default="draft", nullable=False)
    meta_title: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    meta_description: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    keywords: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    og_image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default="now()")
    updated_at: Mapped[datetime] = mapped_column(server_default="now()", onupdate="now()")


class BlogPostCreate(BaseModel):
    title: str
    slug: str
    cover_image_url: Optional[str] = None
    published_date: Optional[date] = None
    read_time: Optional[str] = None
    excerpt: Optional[str] = None
    article_body: Optional[list] = None
    faq: Optional[list] = None
    tags: Optional[list[str]] = None
    status: str = "draft"
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    keywords: Optional[str] = None
    og_image_url: Optional[str] = None


class BlogPostUpdate(BaseModel):
    title: Optional[str] = None
    slug: Optional[str] = None
    cover_image_url: Optional[str] = None
    published_date: Optional[date] = None
    read_time: Optional[str] = None
    excerpt: Optional[str] = None
    article_body: Optional[list] = None
    faq: Optional[list] = None
    tags: Optional[list[str]] = None
    status: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    keywords: Optional[str] = None
    og_image_url: Optional[str] = None


def _row(p: BlogPost) -> dict:
    return {
        "id": str(p.id),
        "title": p.title,
        "slug": p.slug,
        "cover_image_url": p.cover_image_url,
        "published_date": p.published_date.isoformat() if p.published_date else None,
        "read_time": p.read_time,
        "excerpt": p.excerpt,
        "article_body": p.article_body,
        "faq": p.faq,
        "tags": p.tags or [],
        "status": p.status,
        "meta_title": p.meta_title,
        "meta_description": p.meta_description,
        "keywords": p.keywords,
        "og_image_url": p.og_image_url,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


@router.get("")
async def list_blog_posts(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(select(BlogPost).order_by(BlogPost.created_at.desc()))).scalars().all()
    return [_row(p) for p in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_blog_post(
    payload: BlogPostCreate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = (await db.execute(select(BlogPost).where(BlogPost.slug == payload.slug))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Slug already exists")
    post = BlogPost(**payload.model_dump())
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return _row(post)


@router.patch("/{post_id}")
async def update_blog_post(
    post_id: uuid.UUID,
    payload: BlogPostUpdate,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    post = (await db.execute(select(BlogPost).where(BlogPost.id == post_id))).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Blog post not found")
    if payload.slug and payload.slug != post.slug:
        conflict = (await db.execute(select(BlogPost).where(BlogPost.slug == payload.slug))).scalar_one_or_none()
        if conflict:
            raise HTTPException(status_code=409, detail="Slug already exists")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(post, field, value)
    await db.commit()
    await db.refresh(post)
    return _row(post)


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_blog_post(
    post_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    post = (await db.execute(select(BlogPost).where(BlogPost.id == post_id))).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Blog post not found")
    await db.delete(post)
    await db.commit()
