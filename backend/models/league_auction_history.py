"""League Auction History — historical auction prices from the user's league."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


class LeagueAuctionHistory(Base):
    """One row per player per season per source — tracks what opponents actually paid."""
    __tablename__ = "league_auction_history"
    __table_args__ = (
        UniqueConstraint("player_id", "season_year", "source", name="uq_auction_player_season_source"),
        UniqueConstraint("season_year", "source", "yahoo_player_key", name="uq_auction_season_source_yahoo_key"),
        Index("ix_auction_history_season", "season_year"),
        Index("ix_auction_history_player_name_season", "player_name", "season_year"),
        Index("ix_auction_history_user_id", "user_id"),
        Index("ix_auction_history_user_league_id", "user_league_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"), nullable=True)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    team_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 100, not 50: the platform-sync value embeds the user_league id so that one
    # customer's picks cannot collide with another's under the two unique
    # constraints above, neither of which carries a customer or league column.
    # Measured longest value against production: 58 characters.
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    # "yahoo", "manual_csv", or "sync_{user_league_id}_{platform_team_id}"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Row-level security — scope all data to user + league
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True,
    )
    user_league_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("user_leagues.id", ondelete="CASCADE"), nullable=True,
    )

    # Added for multi-year Yahoo sync
    league_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    yahoo_player_key: Mapped[str | None] = mapped_column(String(50), nullable=True)
    player_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    position: Mapped[str | None] = mapped_column(String(10), nullable=True)
    manager_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    draft_pick_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    player = relationship("Player")
