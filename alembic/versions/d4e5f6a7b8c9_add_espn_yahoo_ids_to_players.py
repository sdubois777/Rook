"""add espn_id + real yahoo_id to players

Deterministic ESPN/Yahoo roster resolution: capture the real platform fantasy ids
so a synced roster entry resolves by ID, not fuzzy name.

⚠️ The pre-existing ``yahoo_player_id`` column is a TRAP — it holds "nfl_"+gsis_id,
NOT the Yahoo fantasy player key. It is left UNTOUCHED. This migration adds a NEW,
correctly-named ``yahoo_id`` (the bare numeric Yahoo key) alongside ``espn_id``.
Both nullable + indexed; backfilled by scripts/backfill_platform_ids.py.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('players', sa.Column('espn_id', sa.String(length=50), nullable=True))
    op.add_column('players', sa.Column('yahoo_id', sa.String(length=50), nullable=True))
    op.create_index(op.f('ix_players_espn_id'), 'players', ['espn_id'], unique=False)
    op.create_index(op.f('ix_players_yahoo_id'), 'players', ['yahoo_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_players_yahoo_id'), table_name='players')
    op.drop_index(op.f('ix_players_espn_id'), table_name='players')
    op.drop_column('players', 'yahoo_id')
    op.drop_column('players', 'espn_id')
