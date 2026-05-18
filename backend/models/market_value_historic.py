"""Historic market values — permanent read-only reference of auction prices by season."""
from __future__ import annotations

import uuid

from sqlalchemy import Integer, Numeric, ForeignKey, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


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

    player = relationship("Player", back_populates="historic_prices")

    __table_args__ = (
        UniqueConstraint(
            "player_id", "season_year",
            name="uq_market_value_historic",
        ),
    )
