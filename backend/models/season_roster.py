from __future__ import annotations
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from sqlalchemy import String, Integer, Boolean, DateTime, Numeric, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from backend.database import Base

if TYPE_CHECKING:
    from backend.models.player import Player


class SeasonRoster(Base):
    """Post-draft roster store — promoted from draft bible after draft completes."""
    __tablename__ = "season_roster"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"), nullable=False)
    yahoo_team_id: Mapped[Optional[str]] = mapped_column(String(50))

    # Acquisition details
    acquisition_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    acquisition_week: Mapped[int] = mapped_column(Integer, default=0)  # 0 = draft

    # Weekly tracking (JSON arrays, one entry per week)
    weekly_stats: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    weekly_snap_counts: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    weekly_target_share: Mapped[Optional[list]] = mapped_column(JSONB, default=list)

    # Current season valuations (updated weekly by Roster Monitor agent)
    current_trade_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    value_trend: Mapped[Optional[str]] = mapped_column(String(20))  # rising / falling / stable

    # Flags
    sell_high_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    buy_low_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    injury_concern_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    player: Mapped[Player] = relationship("Player", back_populates="season_roster")
