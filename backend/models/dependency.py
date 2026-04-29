from __future__ import annotations
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from sqlalchemy import String, Integer, Boolean, DateTime, Numeric, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base

if TYPE_CHECKING:
    from backend.models.player import Player


class PlayerDependency(Base):
    """Agent 2 output — dependency flags linking players to each other."""
    __tablename__ = "player_dependencies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"), nullable=False)

    flag_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # displaced / contingent / beneficiary / committee / scheme_fit / college_trust

    trigger_player_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"))
    trigger_player_name: Mapped[Optional[str]] = mapped_column(String(100))
    trigger_condition: Mapped[Optional[str]] = mapped_column(String(50))  # active_and_healthy / injured / absent

    effect_on_value: Mapped[Optional[str]] = mapped_column(String(20))  # negative / positive / neutral
    value_impact_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2))  # e.g. -0.35

    confidence: Mapped[Optional[str]] = mapped_column(String(20))  # high / medium / low
    reasoning: Mapped[Optional[str]] = mapped_column(Text)
    season_year: Mapped[Optional[int]] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    player: Mapped[Player] = relationship(
        "Player", foreign_keys=[player_id], back_populates="dependencies"
    )
    trigger_player: Mapped[Optional[Player]] = relationship(
        "Player", foreign_keys=[trigger_player_id]
    )


class BeatReporterSignal(Base):
    """Agent 6 output — pre-draft and in-season news signals from beat reporters."""
    __tablename__ = "beat_reporter_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"))

    signal_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # practice_limited / depth_chart_change / injury_flag / camp_standout / transaction

    source: Mapped[Optional[str]] = mapped_column(String(100))
    raw_text: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[str]] = mapped_column(String(20))  # high / medium / low
    flagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    player: Mapped[Optional[Player]] = relationship("Player", back_populates="beat_signals")
