"""drop_legacy_league_settings

Revision ID: ce8a4596b413
Revises: 44c2d67384de
Create Date: 2026-05-16 14:20:11.582350

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'ce8a4596b413'
down_revision: Union[str, None] = '44c2d67384de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('league_settings')


def downgrade() -> None:
    op.create_table(
        'league_settings',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('platform', sa.String(50), nullable=False),
        sa.Column('scoring_format', sa.String(10), nullable=False),
        sa.Column('team_count', sa.Integer(), nullable=False),
        sa.Column('auction_budget', sa.Integer(), nullable=False),
        sa.Column('min_bid', sa.Integer(), nullable=False),
        sa.Column('skill_starter_budget', sa.Integer(), nullable=False),
        sa.Column('league_skill_dollar_pool', sa.Integer(), nullable=False),
        sa.Column('total_roster_size', sa.Integer(), nullable=False),
        sa.Column('starting_lineup_size', sa.Integer(), nullable=False),
        sa.Column('roster_slots', JSONB(), nullable=False),
        sa.Column('positional_budget_pcts', JSONB(), nullable=False),
        sa.Column('replacement_level_ppr', JSONB(), nullable=False),
        sa.Column('max_realistic_bid', JSONB(), nullable=False),
        sa.Column('typical_bid_ranges', JSONB(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
