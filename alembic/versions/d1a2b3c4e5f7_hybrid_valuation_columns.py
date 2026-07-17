"""hybrid valuation: rush_efficiency_score + per-format ai_bid_ceiling

Revision ID: d1a2b3c4e5f7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-17

Two additive columns for the market-blind hybrid valuation:
  * player_profiles.rush_efficiency_score — deterministic RB rushing grade (NGS RYOE/att,
    box%-mitigated). Read by the valuation agent context.
  * player_format_values.ai_bid_ceiling — the hybrid per-format auction $ (Half/Standard).
    Read by format_display.overlay_for → draft board / players / detail.
Both nullable; no backfill (populated on the next valuation/profile run).
"""
from alembic import op
import sqlalchemy as sa

revision = "d1a2b3c4e5f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("player_profiles", sa.Column("rush_efficiency_score", sa.String(length=20), nullable=True))
    op.add_column("player_format_values", sa.Column("ai_bid_ceiling", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("player_format_values", "ai_bid_ceiling")
    op.drop_column("player_profiles", "rush_efficiency_score")
