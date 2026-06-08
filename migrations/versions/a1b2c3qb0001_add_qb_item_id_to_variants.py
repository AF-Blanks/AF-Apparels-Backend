"""add qb_item_id to product_variants

Revision ID: a1b2c3qb0001
Revises: z0a1b2c3d4e5
Create Date: 2026-06-08

Stores the QuickBooks Item ID for each product variant so inventory and
invoice line items can reference the matching QB Inventory item.
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3qb0001"
down_revision = "z0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_variants",
        sa.Column("qb_item_id", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_variants", "qb_item_id")
