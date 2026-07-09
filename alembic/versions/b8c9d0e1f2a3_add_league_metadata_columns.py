"""add league metadata columns (draft_date + yahoo settings)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-09

Adds pre-draft league metadata columns to user_leagues that sync previously either
had no home for (draft_date) or fetched and discarded (Yahoo trade_deadline /
waiver_type / playoff_start_week). All nullable — backfilled on the next sync.
"""
from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_leagues", sa.Column("draft_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_leagues", sa.Column("trade_deadline", sa.String(length=50), nullable=True))
    op.add_column("user_leagues", sa.Column("waiver_type", sa.String(length=20), nullable=True))
    op.add_column("user_leagues", sa.Column("playoff_start_week", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_leagues", "playoff_start_week")
    op.drop_column("user_leagues", "waiver_type")
    op.drop_column("user_leagues", "trade_deadline")
    op.drop_column("user_leagues", "draft_date")
