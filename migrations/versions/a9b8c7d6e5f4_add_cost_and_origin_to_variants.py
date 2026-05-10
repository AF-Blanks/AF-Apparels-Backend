"""add_cost_and_origin_to_variants

Revision ID: a9b8c7d6e5f4
Revises: f4a5b6c7d8e9
Create Date: 2026-05-10 00:00:00.000000

Adds cost_per_item and country_of_origin columns to product_variants.
"""

from alembic import op
import sqlalchemy as sa

revision = "a9b8c7d6e5f4"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_variants", sa.Column("cost_per_item", sa.Numeric(10, 2), nullable=True))
    op.add_column("product_variants", sa.Column("country_of_origin", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("product_variants", "country_of_origin")
    op.drop_column("product_variants", "cost_per_item")
