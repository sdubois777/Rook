"""add draft_sessions table (live-draft session isolation + durability)

Revision ID: f6a7b8c9d0e1
Revises: f5a6b7c8d9e0
Create Date: 2026-06-21

One row per user (the session key — one active draft per user). session_state is
the DraftStateManager.to_dict() snapshot, the durable mirror of the in-memory
live engine so a redeploy/crash mid-draft can rehydrate instead of losing the
draft. Deliberately a fresh table, not an extension of the legacy draft_state.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB


# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "draft_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_state", JSONB, nullable=False),
        sa.Column("draft_type", sa.String(20), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Unique → one active draft per user; index supports the per-event lookup.
    op.create_index(
        "ix_draft_sessions_user_id", "draft_sessions", ["user_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_draft_sessions_user_id", table_name="draft_sessions")
    op.drop_table("draft_sessions")
