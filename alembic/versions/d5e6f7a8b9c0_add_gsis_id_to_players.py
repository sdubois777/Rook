"""add gsis_id to players

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-05-12

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("gsis_id", sa.String(20), nullable=True))
    op.create_index("ix_players_gsis_id", "players", ["gsis_id"])
    # Backfill gsis_id from yahoo_player_id (strip "nfl_" prefix)
    op.execute(
        "UPDATE players SET gsis_id = SUBSTRING(yahoo_player_id FROM 5) "
        "WHERE yahoo_player_id LIKE 'nfl_%' AND gsis_id IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_players_gsis_id", "players")
    op.drop_column("players", "gsis_id")
