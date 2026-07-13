"""add user_leagues.my_team_id_source (auto|manual origin for the bind)

Lets a user's MANUAL team pick (recovery when auto-detect fails) survive a later
sync's failed auto-bind — the binder respects a manual origin and never clobbers it.
NULL = never bound. Additive/backward-compatible.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_leagues",
        sa.Column("my_team_id_source", sa.String(length=10), nullable=True),
    )
    # Backfill: existing non-null bindings were all set by the auto-binder.
    op.execute("UPDATE user_leagues SET my_team_id_source = 'auto' WHERE my_team_id IS NOT NULL")


def downgrade() -> None:
    op.drop_column("user_leagues", "my_team_id_source")
