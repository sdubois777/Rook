from __future__ import annotations
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from sqlalchemy import String, Integer, Boolean, DateTime, Numeric, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID, JSONB

from backend.database import Base

if TYPE_CHECKING:
    from backend.models.dependency import PlayerDependency, BeatReporterSignal
    from backend.models.draft_state import OpponentProfile
    from backend.models.season_roster import SeasonRoster


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    yahoo_player_id: Mapped[Optional[str]] = mapped_column(String(50), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    team_abbr: Mapped[Optional[str]] = mapped_column(String(5))
    position: Mapped[Optional[str]] = mapped_column(String(5))  # QB, RB, WR, TE, K, DEF
    age: Mapped[Optional[int]] = mapped_column(Integer)
    contract_year: Mapped[bool] = mapped_column(Boolean, default=False)

    # Top-level valuation (computed from pipeline agents)
    tier: Mapped[Optional[int]] = mapped_column(Integer)
    baseline_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    ceiling_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    floor_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    risk_adjusted_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))

    # Market value (what the room expects to pay)
    market_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    market_value_fantasypros: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    market_value_sleeper: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    market_value_underdog: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    market_value_confidence: Mapped[Optional[str]] = mapped_column(String(20))
    market_value_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Derived fields — gap between system value and market value
    value_gap: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    value_gap_signal: Mapped[Optional[str]] = mapped_column(String(30))  # market_overvalues / market_undervalues / aligned

    # Bid strategy
    recommended_bid_ceiling: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    let_go_threshold: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    elite_anchor_weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))

    # Situation summary
    situation_score: Mapped[Optional[str]] = mapped_column(String(20))  # strong/moderate/weak/volatile
    positional_scarcity_modifier: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))
    breakout_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # Human-readable summary (2-3 sentences, shown during live draft)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Pipeline metadata
    last_pipeline_run: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    data_confidence: Mapped[Optional[str]] = mapped_column(String(20))  # high/medium/low
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    profile: Mapped[Optional[PlayerProfile]] = relationship(
        "PlayerProfile", back_populates="player", uselist=False
    )
    injury_profile: Mapped[Optional[PlayerInjuryProfile]] = relationship(
        "PlayerInjuryProfile", back_populates="player", uselist=False
    )
    schedule: Mapped[Optional[PlayerSchedule]] = relationship(
        "PlayerSchedule", back_populates="player", uselist=False
    )
    dependencies: Mapped[list[PlayerDependency]] = relationship(
        "PlayerDependency", foreign_keys="PlayerDependency.player_id", back_populates="player"
    )
    beat_signals: Mapped[list[BeatReporterSignal]] = relationship(
        "BeatReporterSignal", back_populates="player"
    )
    season_roster: Mapped[Optional[SeasonRoster]] = relationship(
        "SeasonRoster", back_populates="player", uselist=False
    )


class PlayerProfile(Base):
    """Agent 3 output — role classification and efficiency metrics per player per season."""
    __tablename__ = "player_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"), nullable=False)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)

    role_classification: Mapped[Optional[str]] = mapped_column(String(30))
    # WR: wr1_alpha, slot_specialist, deep_threat, possession_wr2, gadget
    # RB: workhorse, early_down_thumper, pass_catching_specialist, committee_back

    # Efficiency metrics
    target_share_3yr_avg: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    target_share_last_season: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    targets_per_route_run: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    air_yards_share: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    snap_percentage: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    separation_score: Mapped[Optional[str]] = mapped_column(String(20))   # elite/above_avg/avg/below_avg
    yards_after_catch_score: Mapped[Optional[str]] = mapped_column(String(20))
    contested_catch_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    efficiency_signal: Mapped[Optional[str]] = mapped_column(String(20))  # elite/above_avg/avg/below_avg

    # Career trajectory
    age_curve_position: Mapped[Optional[str]] = mapped_column(String(20))   # ascending/peak/descending
    career_trajectory: Mapped[Optional[str]] = mapped_column(String(20))

    # Baseline projection from clean seasons
    clean_season_baseline: Mapped[Optional[dict]] = mapped_column(JSONB)
    # {"receptions": 105, "yards": 1320, "touchdowns": 8, "ppr_points": 218}
    anomalous_seasons_excluded: Mapped[Optional[list]] = mapped_column(JSONB, default=list)

    # Breakout
    breakout_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    breakout_reasoning: Mapped[Optional[str]] = mapped_column(Text)
    positional_scarcity_tier: Mapped[Optional[str]] = mapped_column(String(20))  # scarce/moderate/deep

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    player: Mapped[Player] = relationship("Player", back_populates="profile")


class PlayerInjuryProfile(Base):
    """Agent 4 output — risk-adjusted injury profile. One record per player, updated each season."""
    __tablename__ = "player_injury_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"), nullable=False)

    overall_risk_level: Mapped[Optional[str]] = mapped_column(String(20))  # low/moderate/high/volatile
    risk_adjusted_value_modifier: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2))

    injury_log: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    # [{"year": 2024, "injury_type": "hamstring", "category": "soft_tissue", "games_missed": 4, ...}]

    pattern_flags: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    # RECURRING_SOFT_TISSUE, CONCUSSION_HISTORY, HIGH_MILEAGE, POST_ACL, CHRONIC_CONDITION, WORKLOAD_CLIFF

    chronic_conditions: Mapped[Optional[list]] = mapped_column(JSONB, default=list)

    career_carry_count: Mapped[Optional[int]] = mapped_column(Integer)
    workload_cliff_flag: Mapped[bool] = mapped_column(Boolean, default=False)  # 300+ carry season
    high_mileage_flag: Mapped[bool] = mapped_column(Boolean, default=False)    # 600+ career carries
    post_acl_flag: Mapped[bool] = mapped_column(Boolean, default=False)        # within 18 months of ACL
    concussion_count: Mapped[int] = mapped_column(Integer, default=0)

    recovery_assessment: Mapped[Optional[str]] = mapped_column(String(30))  # probable/questionable/doubtful
    age_risk_multiplier: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))
    risk_notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    player: Mapped[Player] = relationship("Player", back_populates="injury_profile")


class PlayerSchedule(Base):
    """Agent 5 output — schedule grades across three windows per player per season."""
    __tablename__ = "player_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("players.id"), nullable=False)
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)

    bye_week: Mapped[Optional[int]] = mapped_column(Integer)
    bye_in_playoff_window: Mapped[bool] = mapped_column(Boolean, default=False)  # bye falls in weeks 14-17

    # Early window (weeks 1-6)
    early_window_grade: Mapped[Optional[str]] = mapped_column(String(20))  # favorable/neutral/tough
    early_window_favorable_weeks: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    early_window_tough_weeks: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    early_window_summary: Mapped[Optional[str]] = mapped_column(Text)

    # Full season
    full_season_grade: Mapped[Optional[str]] = mapped_column(String(20))

    # Playoff window (weeks 14-17) — first-class field, not buried in notes
    playoff_window_grade: Mapped[Optional[str]] = mapped_column(String(20))
    playoff_weeks: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    playoff_matchups: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    playoff_summary: Mapped[Optional[str]] = mapped_column(Text)

    # Weather and context
    weather_risk: Mapped[Optional[str]] = mapped_column(String(20))  # low/moderate/high
    weather_affected_weeks: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    divisional_game_weeks: Mapped[Optional[list]] = mapped_column(JSONB, default=list)

    schedule_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 1))
    schedule_notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    player: Mapped[Player] = relationship("Player", back_populates="schedule")
