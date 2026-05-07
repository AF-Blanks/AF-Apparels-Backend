"""Make order_items.variant_id nullable with SET NULL on delete.

Revision ID: u4v5w6x7y8z9
Revises: t3u4v5w6x7y8
Create Date: 2026-05-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "u4v5w6x7y8z9"
down_revision: Union[str, None] = "t3u4v5w6x7y8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old RESTRICT foreign key constraint
    op.drop_constraint("order_items_variant_id_fkey", "order_items", type_="foreignkey")
    # Make the column nullable
    op.alter_column("order_items", "variant_id", nullable=True)
    # Re-add the foreign key with SET NULL
    op.create_foreign_key(
        "order_items_variant_id_fkey",
        "order_items",
        "product_variants",
        ["variant_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("order_items_variant_id_fkey", "order_items", type_="foreignkey")
    op.alter_column("order_items", "variant_id", nullable=False)
    op.create_foreign_key(
        "order_items_variant_id_fkey",
        "order_items",
        "product_variants",
        ["variant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
