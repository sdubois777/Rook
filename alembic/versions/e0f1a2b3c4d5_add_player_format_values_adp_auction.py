"""add per-format ADP + auction columns to player_format_values (G5 ingest)

Additive/nullable so each of the 3 format rows carries its own re-scraped ADP
(FantasyPros overall rank, same scale as players.adp_fantasypros) and auction $
(DraftWizard, canonical 12-team/1-flex roster). auction_roster_shape records the
roster assumptions used so Phase 2 can disclose them to non-canonical leagues.
No rewrite of existing rows. Nothing reads these yet (inert until Phase 2).

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""
from alembic import op
import sqlalchemy as sa

revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("player_format_values", sa.Column("adp_fantasypros", sa.Numeric(5, 1), nullable=True))
    op.add_column("player_format_values", sa.Column("auction_value", sa.Numeric(5, 2), nullable=True))
    op.add_column("player_format_values", sa.Column("auction_roster_shape", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("player_format_values", "auction_roster_shape")
    op.drop_column("player_format_values", "auction_value")
    op.drop_column("player_format_values", "adp_fantasypros")
