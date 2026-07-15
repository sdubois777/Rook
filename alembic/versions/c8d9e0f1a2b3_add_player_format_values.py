"""add player_format_values (per-scoring-format valuation rows)

Additive table holding one row per (player, scoring_format) for per-format
(PPR/Half/Standard) support. The PPR row mirrors the players table; Half/Standard
are the repriced values. No rewrite of existing data.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "c8d9e0f1a2b3"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "player_format_values",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("player_id", UUID(as_uuid=True),
                  sa.ForeignKey("players.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scoring_format", sa.String(length=10), nullable=False),
        sa.Column("projected_points", sa.Numeric(6, 1), nullable=True),
        sa.Column("replacement_ppr", sa.Numeric(6, 1), nullable=True),
        sa.Column("tier", sa.Integer(), nullable=True),
        sa.Column("baseline_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("recommended_bid_ceiling", sa.Numeric(5, 2), nullable=True),
        sa.Column("ceiling_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("floor_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("risk_adjusted_value", sa.Numeric(5, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("player_id", "scoring_format", name="uq_player_format"),
    )
    op.create_index(
        "ix_player_format_values_player_id", "player_format_values", ["player_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_player_format_values_player_id", table_name="player_format_values")
    op.drop_table("player_format_values")
