"""add ADP fields to players (fantasypros + ai + scoring format)

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-06-16

Snake-draft support: store scraped FantasyPros ADP, an AI-generated snake ADP
(pick number, produced by valuation_agent alongside ai_bid_ceiling), and the
scoring format the ADP was fetched for ("ppr" | "half_ppr" | "standard").
All nullable — populated by a later pipeline run; null renders as "--"/N/A.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "players",
        sa.Column("adp_fantasypros", sa.Numeric(5, 1), nullable=True),
    )
    op.add_column(
        "players",
        sa.Column("adp_ai", sa.Numeric(5, 1), nullable=True),
    )
    op.add_column(
        "players",
        sa.Column("adp_scoring", sa.String(10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("players", "adp_scoring")
    op.drop_column("players", "adp_ai")
    op.drop_column("players", "adp_fantasypros")
