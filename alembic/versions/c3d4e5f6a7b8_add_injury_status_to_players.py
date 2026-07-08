"""add injury_status + injury_status_updated_at to players

Revision ID: c3d4e5f6a7b8
Revises: b2d3e4f5a6c7
Create Date: 2026-07-09

Live injury designation for the status badge (canonical code "Q" | "D" | "O" |
"IR"; NULL = healthy), sourced from Sleeper injury_status during sync_rosters
(sleeper_id join) and refreshed daily. Display-only — NOT a valuation input.
Both nullable; no backfill (populated on the next roster sync).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c3d4e5f6a7b8"
down_revision = "b2d3e4f5a6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("injury_status", sa.String(4), nullable=True))
    op.add_column(
        "players",
        sa.Column("injury_status_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("players", "injury_status_updated_at")
    op.drop_column("players", "injury_status")
