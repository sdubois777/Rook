"""Historic market values — season-keyed reference of what a player cost.

TWO KINDS OF NUMBER LIVE HERE, and ``source`` is what tells them apart:

  * ``league_auction``       — what a real league actually paid. This is the only kind
                               a backtest may score against, because every buy/sell
                               signal is our bid ceiling minus "the market", and
                               scoring a projection against another projection measures
                               nothing about the market.
  * ``fantasypros_consensus`` — a preseason estimate, archived by
                               backend/engines/market_values.py before it overwrites
                               the live price column.

Readers must state which kind they want. Before ``source`` existed the two were written
into the same price column with no way to distinguish them.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Integer, Numeric, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base

# The only valid values of MarketValueHistoric.source.
SOURCE_LEAGUE_AUCTION = "league_auction"
SOURCE_FANTASYPROS_CONSENSUS = "fantasypros_consensus"
REALIZED_SOURCES = (SOURCE_LEAGUE_AUCTION,)


class MarketValueHistoric(Base):
    __tablename__ = "market_value_historic"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("players.id"),
        nullable=False,
        index=True,
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    # How this price was obtained — see the module docstring. Defaults to a realized
    # league auction so any row written by code predating this column is classified
    # as what those rows actually were.
    source: Mapped[str] = mapped_column(
        String(32), nullable=False,
        server_default=SOURCE_LEAGUE_AUCTION, index=True,
    )

    player = relationship("Player", back_populates="historic_prices")

    __table_args__ = (
        # source is part of the key so a realized price and an estimate can coexist for
        # one player and season. With the narrower (player_id, season_year) key an
        # estimate that landed first became that season's price of record permanently.
        UniqueConstraint(
            "player_id", "season_year", "source",
            name="uq_market_value_historic",
        ),
    )
