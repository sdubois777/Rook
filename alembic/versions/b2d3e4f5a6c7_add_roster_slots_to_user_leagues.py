"""add roster_slots to user_leagues

Per-league starting-lineup config (T3). Nullable JSONB holding the NORMALIZED
canonical model ({slot_type: count}), not raw platform data. NULL = not fetched
→ every consumer uses the byte-unchanged default lineup (additive by design).

Revision ID: b2d3e4f5a6c7
Revises: a1c2e3f4b5d6
Create Date: 2026-07-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b2d3e4f5a6c7'
down_revision: Union[str, None] = 'a1c2e3f4b5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_leagues',
        sa.Column('roster_slots', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('user_leagues', 'roster_slots')
