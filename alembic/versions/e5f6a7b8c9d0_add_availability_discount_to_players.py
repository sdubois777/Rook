"""add pre-draft availability discount columns to players

Deterministic games-missed proration for a KNOWN current multi-week absence
(PUP / long-term IR / suspension). The draft-ranked value reads base ×
availability_factor. Base value fields are untouched (idempotent recompute).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('players', sa.Column(
        'availability_factor', sa.Numeric(4, 3), nullable=False, server_default='1.000'))
    op.add_column('players', sa.Column(
        'availability_games_missed', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('players', sa.Column(
        'availability_reason', sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column('players', 'availability_reason')
    op.drop_column('players', 'availability_games_missed')
    op.drop_column('players', 'availability_factor')
