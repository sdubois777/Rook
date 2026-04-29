from __future__ import annotations
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from backend.database import Base


class DraftState(Base):
    """Live draft session state — one record per active draft."""
    __tablename__ = "draft_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)
    league_id: Mapped[Optional[str]] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # Budget tracking
    total_budget: Mapped[int] = mapped_column(Integer, default=200)
    my_budget_remaining: Mapped[int] = mapped_column(Integer, default=200)

    # Picks made — list of {yahoo_player_id, name, position, price, round}
    my_roster: Mapped[Optional[list]] = mapped_column(JSONB, default=list)

    # Current board state
    current_nominee_yahoo_id: Mapped[Optional[str]] = mapped_column(String(50))
    current_bid_amount: Mapped[Optional[int]] = mapped_column(Integer)
    clock_seconds_remaining: Mapped[Optional[int]] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    opponents: Mapped[list[OpponentProfile]] = relationship(
        "OpponentProfile", back_populates="draft_state", cascade="all, delete-orphan"
    )


class OpponentProfile(Base):
    """Per-opponent tracking during live draft and in-season."""
    __tablename__ = "opponent_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    draft_state_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("draft_state.id"), nullable=True
    )
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)

    yahoo_team_id: Mapped[Optional[str]] = mapped_column(String(50))
    team_name: Mapped[str] = mapped_column(String(100), nullable=False)

    # Draft state
    roster: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    budget_remaining: Mapped[Optional[int]] = mapped_column(Integer)
    budget_spent: Mapped[Optional[int]] = mapped_column(Integer)

    # Strength analysis
    positional_scores: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    # {"QB": 0.2, "RB": 0.9, "WR": 0.4, "TE": 0.3}

    threat_score: Mapped[Optional[int]] = mapped_column(Integer)  # 0-100
    combo_flags: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    apparent_strategy: Mapped[Optional[str]] = mapped_column(String(30))
    # zero_rb / hero_rb / balanced / positional_run

    likely_targets: Mapped[Optional[list]] = mapped_column(JSONB, default=list)

    # In-season management style (Phase 3)
    management_style: Mapped[Optional[str]] = mapped_column(String(30))
    # reactive / analytical / name_brand_biased / urgency_driven
    current_record: Mapped[Optional[str]] = mapped_column(String(10))  # "5-3"
    trade_history: Mapped[Optional[list]] = mapped_column(JSONB, default=list)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    draft_state: Mapped[Optional[DraftState]] = relationship("DraftState", back_populates="opponents")
