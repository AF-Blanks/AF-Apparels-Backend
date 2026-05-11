"""retail_customer_accounts

Revision ID: b1c2d3e5f6a7
Revises: a9b8c7d6e5f4
Create Date: 2026-05-10 00:00:00.000000

Adds retail account fields to users table and makes hashed_password nullable
so retail users can be created before they set a password.

# Raw SQL equivalent:
# ALTER TABLE users ADD COLUMN account_type VARCHAR(20) NOT NULL DEFAULT 'wholesale';
# ALTER TABLE users ADD COLUMN activation_token VARCHAR(255);
# ALTER TABLE users ADD COLUMN activation_token_expires TIMESTAMPTZ;
# ALTER TABLE users ALTER COLUMN hashed_password DROP NOT NULL;
"""

from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e5f6a7"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("account_type", sa.String(20), nullable=False, server_default="wholesale"),
    )
    op.add_column(
        "users",
        sa.Column("activation_token", sa.String(255), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("activation_token_expires", sa.DateTime(timezone=True), nullable=True),
    )
    # Allow NULL hashed_password for retail users who haven't set a password yet
    op.alter_column("users", "hashed_password", nullable=True)


def downgrade() -> None:
    # Re-fill any NULL hashed_password before restoring NOT NULL constraint
    op.execute("UPDATE users SET hashed_password = '' WHERE hashed_password IS NULL")
    op.alter_column("users", "hashed_password", nullable=False)
    op.drop_column("users", "activation_token_expires")
    op.drop_column("users", "activation_token")
    op.drop_column("users", "account_type")
