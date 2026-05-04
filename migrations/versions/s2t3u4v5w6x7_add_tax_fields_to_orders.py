"""Add tax_rate and tax_region columns to orders table.

Revision ID: s2t3u4v5w6x7
Revises: r1s2t3u4v5w6
Create Date: 2026-05-04 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s2t3u4v5w6x7"
down_revision: Union[str, None] = "r1s2t3u4v5w6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("tax_rate", sa.Numeric(6, 4), nullable=True))
    op.add_column("orders", sa.Column("tax_region", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "tax_region")
    op.drop_column("orders", "tax_rate")
