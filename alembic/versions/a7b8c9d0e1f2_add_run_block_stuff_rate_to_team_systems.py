"""add run_block_stuff_rate to team_systems

The real run-blocking numeric (Teams rework slice 2): stuff rate = runs stopped
at/behind the LOS (tackled_for_loss) / total runs, per team. Lower = better OL.
Stored alongside sack_rate; run_blocking_grade ranks on it (slice 3 applies the
widened-bell curve).

Revision ID: a7b8c9d0e1f2
Revises: e5f6a7b8c9d0
Create Date: 2026-07-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('team_systems', sa.Column('run_block_stuff_rate', sa.Numeric(5, 4), nullable=True))


def downgrade() -> None:
    op.drop_column('team_systems', 'run_block_stuff_rate')
