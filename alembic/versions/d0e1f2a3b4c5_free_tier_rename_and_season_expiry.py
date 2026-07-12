"""free tier rename (intro->free) + season entitlement expiry column

The tier/credit spec collapse: 'intro' becomes 'free' (it was already the
de-facto free tier — signup granted it without payment), and season purchases
need an expiry instant (tier_expires_at; NULL = monthly-managed or free).

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
"""
from alembic import op
import sqlalchemy as sa

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("tier_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Rename the tier value for every existing user + the column default.
    op.execute("UPDATE users SET tier = 'free' WHERE tier = 'intro'")
    op.alter_column("users", "tier", server_default="free")


def downgrade() -> None:
    op.execute("UPDATE users SET tier = 'intro' WHERE tier = 'free'")
    op.alter_column("users", "tier", server_default="intro")
    op.drop_column("users", "tier_expires_at")
