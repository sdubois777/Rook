"""add draft metadata columns

Revision ID: a1b2c3d4e5f6
Revises: b5c9d3e2f6a7
Create Date: 2026-05-09

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "b5c9d3e2f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("draft_round", sa.Integer(), nullable=True))
    op.add_column("players", sa.Column("draft_pick", sa.Integer(), nullable=True))
    op.add_column("players", sa.Column("draft_year", sa.Integer(), nullable=True))
    op.add_column("players", sa.Column("nfl_seasons_played", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "nfl_seasons_played")
    op.drop_column("players", "draft_year")
    op.drop_column("players", "draft_pick")
    op.drop_column("players", "draft_round")
