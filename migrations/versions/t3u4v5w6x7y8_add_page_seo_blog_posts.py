"""Add page_seo and blog_posts tables.

Revision ID: t3u4v5w6x7y8
Revises: s2t3u4v5w6x7
Create Date: 2026-05-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "t3u4v5w6x7y8"
down_revision: Union[str, None] = "s2t3u4v5w6x7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "page_seo",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("page_slug", sa.String(100), nullable=False),
        sa.Column("meta_title", sa.String(60), nullable=True),
        sa.Column("meta_description", sa.String(160), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("og_image_url", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_slug"),
    )

    op.create_table(
        "blog_posts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("cover_image_url", sa.Text(), nullable=True),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("read_time", sa.String(50), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("article_body", postgresql.JSONB(), nullable=True),
        sa.Column("faq", postgresql.JSONB(), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("meta_title", sa.String(60), nullable=True),
        sa.Column("meta_description", sa.String(160), nullable=True),
        sa.Column("keywords", sa.Text(), nullable=True),
        sa.Column("og_image_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )


def downgrade() -> None:
    op.drop_table("blog_posts")
    op.drop_table("page_seo")
