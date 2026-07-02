"""granted_pack_sessions

Idempotency table for one-time credit-pack grants — keyed on the Stripe Checkout
session id (§6), so a pack's credits are granted exactly once per completed
session even if the completed event is redelivered under a different event id.

Revision ID: f8a9b0c1d2e3
Revises: f7a8b9c0d1e2
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'granted_pack_sessions',
        sa.Column('session_id', sa.String(length=255), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('credits', sa.Integer(), nullable=False),
        sa.Column(
            'granted_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('session_id'),
    )
    op.create_index(
        op.f('ix_granted_pack_sessions_user_id'),
        'granted_pack_sessions',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_granted_pack_sessions_user_id'),
        table_name='granted_pack_sessions',
    )
    op.drop_table('granted_pack_sessions')
