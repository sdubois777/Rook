"""add_depth_chart_order_to_players

Revision ID: d7e8f9a0b1c2
Revises: cc060e304351
Create Date: 2026-05-17 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b1c2"
down_revision: str = "cc060e304351"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "players",
        sa.Column("depth_chart_order", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("players", "depth_chart_order")
