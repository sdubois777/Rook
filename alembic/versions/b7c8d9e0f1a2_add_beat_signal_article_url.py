"""add beat_reporter_signals.article_url (per-article source permalink)

The beat-reporter RSS ingestion already fetched each entry's `link` but dropped
it on write, so the news feed could show a source name ("via ESPN") but not link
out. This adds a nullable column to persist the article permalink; the agent now
writes it and the /news API exposes it. NULL for legacy rows (non-clickable).
Additive/backward-compatible.

Revision ID: b7c8d9e0f1a2
Revises: f2a3b4c5d6e7
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c8d9e0f1a2"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "beat_reporter_signals",
        sa.Column("article_url", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("beat_reporter_signals", "article_url")
