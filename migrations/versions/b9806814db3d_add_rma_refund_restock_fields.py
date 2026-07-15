"""add refund/restock tracking fields to rma_requests

Revision ID: b9806814db3d
Revises: a1b2c3qb0001
Create Date: 2026-07-15

Tracks whether an approved RMA's payment refund and inventory restock
actually succeeded, and the QuickBooks Payments refund ID for audit.
"""
from alembic import op
import sqlalchemy as sa

revision = "b9806814db3d"
down_revision = "a1b2c3qb0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rma_requests", sa.Column("refund_status", sa.String(20), nullable=True))
    op.add_column("rma_requests", sa.Column("refund_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("rma_requests", sa.Column("qb_refund_id", sa.String(255), nullable=True))
    op.add_column("rma_requests", sa.Column("restock_status", sa.String(20), nullable=True))
    op.add_column("rma_requests", sa.Column("restocked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("rma_requests", sa.Column("processing_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rma_requests", "processing_error")
    op.drop_column("rma_requests", "restocked_at")
    op.drop_column("rma_requests", "restock_status")
    op.drop_column("rma_requests", "qb_refund_id")
    op.drop_column("rma_requests", "refund_amount")
    op.drop_column("rma_requests", "refund_status")
