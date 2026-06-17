"""add snake fields to players (adp_rank, adp_diff, snake_flag)

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-06-17

Snake-draft polish: a clean 1-N AI ranking (adp_rank), the consensus-vs-us
differential (adp_diff = adp_fantasypros - adp_ai; positive = we rate higher),
and a snake draft flag (VALUE | SLEEPER | TARGET | REACH). All nullable —
populated by a valuation_agent run.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("players", sa.Column("adp_rank", sa.Integer(), nullable=True))
    op.add_column("players", sa.Column("adp_diff", sa.Numeric(5, 1), nullable=True))
    op.add_column("players", sa.Column("snake_flag", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "snake_flag")
    op.drop_column("players", "adp_diff")
    op.drop_column("players", "adp_rank")
