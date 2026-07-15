"""add value_assessment + auction_note to player_format_values (per-format prose)

Additive/nullable prose columns so each of the 3 format rows carries its own
value_assessment + auction_note (G2). No rewrite of existing rows.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""
from alembic import op
import sqlalchemy as sa

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("player_format_values", sa.Column("value_assessment", sa.String(length=20), nullable=True))
    op.add_column("player_format_values", sa.Column("auction_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("player_format_values", "auction_note")
    op.drop_column("player_format_values", "value_assessment")
