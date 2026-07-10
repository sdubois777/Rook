"""add my_team_id (is_me binding) to user_leagues

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-10

The user's OWN team within a synced league, bound by exact owner-identity on every sync
(Sleeper owner_id/co_owners, ESPN SWID vs owners[], Yahoo is_owned_by_current_login).
NULL = no team matched the user's identity (fail-loud, never a positional guess).
"""
from alembic import op
import sqlalchemy as sa


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_leagues", sa.Column("my_team_id", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("user_leagues", "my_team_id")
