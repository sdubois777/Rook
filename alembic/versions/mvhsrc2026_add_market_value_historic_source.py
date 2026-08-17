"""add source column to market_value_historic

WHY THIS EXISTS. market_value_historic answers "what did this player cost in season N".
Two completely different kinds of number were being written into that one price column
with nothing to tell them apart:

  * REALIZED AUCTION PRICES — what a real league actually paid. The 159 rows this
    deployment holds for 2025 are of this kind, and backend/engines/backtest.py scores
    our board against them.
  * PRESEASON CONSENSUS ESTIMATES — what FantasyPros projects a player will go for.
    backend/engines/market_values.py writes these when it archives the outgoing price
    before a scrape.

A consensus estimate standing in for a realized price is not a small error: every
buy/sell signal is computed as our bid ceiling minus "the market", so scoring against
a projection measures our board against another projection rather than against what
the league paid.

The unique key widens from (player_id, season_year) to (player_id, season_year, source)
so both kinds can coexist for one player and season, and each reader states which it
wants. Existing rows are backfilled as realized auction prices, which is what they are.

Revision ID: mvhsrc2026
Revises: ref2026email
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "mvhsrc2026"
down_revision: Union[str, None] = "ref2026email"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in lockstep with backend/models/market_value_historic.py.
SOURCE_LEAGUE_AUCTION = "league_auction"


def upgrade() -> None:
    # server_default is REQUIRED on a NOT NULL add to an existing table — without it the
    # backfill of existing rows has no value to use and the migration fails. It also
    # makes the correct classification the default for anything inserted by code that
    # predates this column.
    op.add_column(
        "market_value_historic",
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default=SOURCE_LEAGUE_AUCTION,
        ),
    )

    # Widen the unique key so a realized price and an estimate can coexist for the same
    # player and season. Dropping first is safe: the new constraint is strictly weaker,
    # so no existing row can violate it.
    op.drop_constraint(
        "uq_market_value_historic", "market_value_historic", type_="unique"
    )
    op.create_unique_constraint(
        "uq_market_value_historic",
        "market_value_historic",
        ["player_id", "season_year", "source"],
    )
    op.create_index(
        "ix_market_value_historic_source", "market_value_historic", ["source"]
    )


def downgrade() -> None:
    # Estimates have no place in a table keyed only by (player, season): leaving them
    # would let one silently become that season's price of record. Remove them before
    # narrowing the key back, so the restored constraint cannot be violated either.
    op.execute(
        "DELETE FROM market_value_historic WHERE source <> '%s'" % SOURCE_LEAGUE_AUCTION
    )
    op.drop_index("ix_market_value_historic_source", table_name="market_value_historic")
    op.drop_constraint(
        "uq_market_value_historic", "market_value_historic", type_="unique"
    )
    op.create_unique_constraint(
        "uq_market_value_historic",
        "market_value_historic",
        ["player_id", "season_year"],
    )
    op.drop_column("market_value_historic", "source")
