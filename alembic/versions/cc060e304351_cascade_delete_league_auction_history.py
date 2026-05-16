"""cascade_delete_league_auction_history

Revision ID: cc060e304351
Revises: 742ec39b8039
Create Date: 2026-05-16 15:03:21.688923

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cc060e304351'
down_revision: Union[str, None] = '742ec39b8039'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_auction_history_user_league_id",
        "league_auction_history",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_auction_history_user_league_id",
        "league_auction_history",
        "user_leagues",
        ["user_league_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_auction_history_user_league_id",
        "league_auction_history",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_auction_history_user_league_id",
        "league_auction_history",
        "user_leagues",
        ["user_league_id"],
        ["id"],
        ondelete="SET NULL",
    )
