"""create market_value_historic table

Revision ID: c1d2e3f4a5b6
Revises: b9c0d1e2f3a4
Create Date: 2026-05-18

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "c1d2e3f4a5b6"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_value_historic",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "player_id", UUID(as_uuid=True),
            sa.ForeignKey("players.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("season_year", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(8, 2), nullable=False),
        sa.UniqueConstraint(
            "player_id", "season_year",
            name="uq_market_value_historic",
        ),
    )


def downgrade() -> None:
    op.drop_table("market_value_historic")
