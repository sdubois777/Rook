"""add suspended_at to user_leagues

Tier-cap suspension: a current-season league parked over the cap by a downgrade.
Distinct from is_active (season). Nullable — NULL = not parked. Data stays intact.

Revision ID: a1c2e3f4b5d6
Revises: f8a9b0c1d2e3
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1c2e3f4b5d6'
down_revision: Union[str, None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_leagues',
        sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user_leagues', 'suspended_at')
