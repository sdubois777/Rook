"""add user_leagues.draft_status (explicit Sleeper draft status)

Sleeper's draft object carries a status (pre_draft | drafting | complete) that the
sync fetches but previously dropped. Persisting it gives an EXPLICIT undrafted signal
for Sleeper (whose draft_date is null); ESPN/Yahoo stay on draft_date. NULL = unknown.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_leagues",
        sa.Column("draft_status", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_leagues", "draft_status")
