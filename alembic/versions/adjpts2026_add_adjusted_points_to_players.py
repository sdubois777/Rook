"""add adjusted_points to players

The board displayed RAW projected points next to dollars derived from ADJUSTED points
(raw x injury discount x dependency adjustment), so a receiver projected fewer points
could be priced higher. This column persists the quantity the dollars are actually
computed from, so the PPR surfaces can display it.

Nullable with no server default: NULL means "not valued this run", which is exactly what
the stale-value sweep in run_valuation_pass writes and what K/DEF carry (no projection
chain at all). The read sites fall back to the raw projection when it is NULL.

Revision ID: adjpts2026
Revises: vs2026snap01
Create Date: 2026-07-24

"""
from alembic import op
import sqlalchemy as sa


revision = "adjpts2026"
down_revision = "vs2026snap01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "players",
        sa.Column("adjusted_points", sa.Numeric(6, 1), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("players", "adjusted_points")
