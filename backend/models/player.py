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
    from backend.models.market_value_historic import MarketValueHistoric
    from backend.models.season_roster import SeasonRoster


class Player(Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    yahoo_player_id: Mapped[Optional[str]] = mapped_column(String(50), unique=True, nullable=True)
    gsis_id: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    sportradar_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    sleeper_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    team_abbr: Mapped[Optional[str]] = mapped_column(String(5))
    position: Mapped[Optional[str]] = mapped_column(String(5))  # QB, RB, WR, TE, K, DEF
    age: Mapped[Optional[int]] = mapped_column(Integer)
    depth_chart_order: Mapped[Optional[int]] = mapped_column(Integer)
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
    market_value_league: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    market_value_prior_season: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    market_value_prior_season_year: Mapped[Optional[int]] = mapped_column(Integer)
    market_value_confidence: Mapped[Optional[str]] = mapped_column(String(20))
    market_value_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ADP — average draft position for snake drafts (lower = earlier pick).
    # adp_fantasypros: scraped consensus. adp_ai: valuation_agent's snake pick.
    # adp_scoring: format the ADP was fetched for ("ppr" | "half_ppr" | "standard").
    adp_fantasypros: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    adp_ai: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    adp_scoring: Mapped[Optional[str]] = mapped_column(String(10))

    # Derived fields — gap between system value and market value
    value_gap: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    value_gap_signal: Mapped[Optional[str]] = mapped_column(String(30))  # market_overvalues / market_undervalues / aligned

    # Bid strategy
    recommended_bid_ceiling: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    let_go_threshold: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    elite_anchor_weight: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))

    # AI valuation agent output
    ai_bid_ceiling: Mapped[Optional[int]] = mapped_column(Integer)
    ai_confidence_floor: Mapped[Optional[int]] = mapped_column(Integer)
    ai_confidence_ceiling: Mapped[Optional[int]] = mapped_column(Integer)
    value_assessment: Mapped[Optional[str]] = mapped_column(String(20))  # elite_value/good_value/fair_value/slight_overpay/avoid
    auction_note: Mapped[Optional[str]] = mapped_column(Text)
    pay_up_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    nomination_target_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # Situation summary
    situation_score: Mapped[Optional[str]] = mapped_column(String(20))  # strong/moderate/weak/volatile
    positional_scarcity_modifier: Mapped[Optional[Decimal]] = mapped_column(Numeric(3, 2))
    breakout_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # Draft metadata
    draft_round: Mapped[Optional[int]] = mapped_column(Integer)
    draft_pick: Mapped[Optional[int]] = mapped_column(Integer)       # Overall pick number
    draft_year: Mapped[Optional[int]] = mapped_column(Integer)
    nfl_seasons_played: Mapped[Optional[int]] = mapped_column(Integer)

    # Rookie evaluation fields (written by Agent 2: Roster Changes)
    is_rookie: Mapped[bool] = mapped_column(Boolean, default=False)
    college_profile_grade: Mapped[Optional[str]] = mapped_column(String(20))     # elite/strong/average/weak
    draft_capital_signal: Mapped[Optional[str]] = mapped_column(String(10))      # high/medium/low
    draft_capital_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    adjusted_dominator_rating: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    conference: Mapped[Optional[str]] = mapped_column(String(30))
    historical_comp_names: Mapped[Optional[list]] = mapped_column(JSONB)
    comp_yr1_avg_ppg: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    comp_yr2_avg_ppg: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    landing_spot_modifier: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3))
    projection_confidence: Mapped[Optional[str]] = mapped_column(String(10))    # low/medium/high
    variance_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # Human-readable summary (2-3 sentences, shown during live draft)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    # Team change tracking (for profile cache invalidation)
    team_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

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
    historic_prices: Mapped[list[MarketValueHistoric]] = relationship(
        "MarketValueHistoric", back_populates="player"
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
    projection_reasoning: Mapped[Optional[str]] = mapped_column(Text)
    positional_scarcity_tier: Mapped[Optional[str]] = mapped_column(String(20))  # scarce/moderate/deep

    # Rookie-specific profile fields (populated when is_rookie=True)
    is_rookie: Mapped[bool] = mapped_column(Boolean, default=False)
    profile_source: Mapped[Optional[str]] = mapped_column(String(20))   # nfl_history/college_comps
    ceiling_value_ppr: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 1))
    floor_value_ppr: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 1))
    confidence: Mapped[Optional[str]] = mapped_column(String(10))       # low/medium/high
    variance_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    breakout_window: Mapped[Optional[str]] = mapped_column(String(20))  # year_1/year_2_to_3/year_2_to_4/year_3_to_4
    year1_role: Mapped[Optional[str]] = mapped_column(String(20))       # starter/rotational/depth

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

    # ── Games-based availability model (objective, derived from games played) ──
    games_played_history: Mapped[Optional[list]] = mapped_column(JSONB, default=list)
    # [{"season": 2023, "games": 16, "full_season": true}, ...]
    avg_games_per_season: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 1))
    projected_games: Mapped[Optional[int]] = mapped_column(Integer)
    availability_risk: Mapped[Optional[str]] = mapped_column(String(20))    # durable/monitor/concern/unknown
    availability_trend: Mapped[Optional[str]] = mapped_column(String(20))   # improving/declining/stable/volatile
    availability_risk_modifier: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 2))
    full_season_absence_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

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
    bye_in_playoff_window: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")  # bye falls in weeks 14-17

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
