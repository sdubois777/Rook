"""add_draft_token_to_users

Revision ID: 42e87b841448
Revises: c1d2e3f4a5b6
Create Date: 2026-05-24 14:37:59.699751

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '42e87b841448'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('draft_token', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_users_draft_token'), 'users', ['draft_token'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_draft_token'), table_name='users')
    op.drop_column('users', 'draft_token')
