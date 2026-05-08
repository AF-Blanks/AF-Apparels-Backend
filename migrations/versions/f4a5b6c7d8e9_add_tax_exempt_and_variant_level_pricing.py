"""add_tax_exempt_and_variant_level_pricing

Revision ID: f4a5b6c7d8e9
Revises: e1f2a3b4c5d6
Create Date: 2026-05-08 00:00:00.000000

Adds tax_exempt boolean column to companies.
Adds variant_level_pricing_overrides table for per-variant, per-group price overrides.
"""

from alembic import op
import sqlalchemy as sa

revision = "f4a5b6c7d8e9"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("tax_exempt", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.create_table(
        "variant_level_pricing_overrides",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_id", sa.String(36), nullable=False, index=True),
        sa.Column("group_id", sa.String(36), nullable=False, index=True),
        sa.Column("price", sa.Numeric(10, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vlpo_variant_id", "variant_level_pricing_overrides", ["variant_id"])
    op.create_index("ix_vlpo_group_id", "variant_level_pricing_overrides", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_vlpo_group_id", table_name="variant_level_pricing_overrides")
    op.drop_index("ix_vlpo_variant_id", table_name="variant_level_pricing_overrides")
    op.drop_table("variant_level_pricing_overrides")
    op.drop_column("companies", "tax_exempt")
