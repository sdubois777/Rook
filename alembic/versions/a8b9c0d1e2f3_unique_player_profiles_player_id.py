"""unique player_profiles player_id

Revision ID: a8b9c0d1e2f3
Revises: d7e8f9a0b1c2
Create Date: 2026-05-18

Dedup player_profiles (keep newest per player_id) then add unique constraint.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "a8b9c0d1e2f3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Delete duplicate player_profiles rows, keeping the one with the latest updated_at
    op.execute("""
        DELETE FROM player_profiles
        WHERE id NOT IN (
            SELECT DISTINCT ON (player_id) id
            FROM player_profiles
            ORDER BY player_id, updated_at DESC
        )
    """)

    op.create_unique_constraint(
        "uq_player_profiles_player_id", "player_profiles", ["player_id"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_player_profiles_player_id", "player_profiles", type_="unique"
    )
