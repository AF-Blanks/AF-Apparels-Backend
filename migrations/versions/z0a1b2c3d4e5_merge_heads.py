"""merge heads: retail_customer_accounts + add_ach_account_to_companies

Revision ID: z0a1b2c3d4e5
Revises: b1c2d3e5f6a7, v5w6x7y8z9a0
Create Date: 2026-06-01

Merges two independent migration branches so Alembic has a single head
and `alembic upgrade head` works without the 'Multiple head revisions' error.
"""
from alembic import op
import sqlalchemy as sa

revision = "z0a1b2c3d4e5"
down_revision = ("b1c2d3e5f6a7", "v5w6x7y8z9a0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
