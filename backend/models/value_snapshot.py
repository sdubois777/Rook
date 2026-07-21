"""ValueSnapshot — an IMMUTABLE, append-only record of what the engine said about a
player BEFORE a season was played, alongside the market and the engine identity that
produced it. Written back with the actual outcome after the season.

The point: the product claim is "our value vs the market — the GAP is the edge." That is
unfalsifiable without a locked pre-season prediction to score against actuals. This table
is that lock. Rows are never updated (except the outcome write-back), so a later pipeline
run can never rewrite history.

Identity survives player-row merges (players.id is NOT merge-stable — merged rows are
deleted): gsis_id is the primary cross-season + outcome-join key (it is exactly what
nfl_data_py actuals key on); player_id is a soft reference (ON DELETE SET NULL).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class ValueSnapshot(Base):
    __tablename__ = "value_snapshots"
    __table_args__ = (
        # Immutability anchor keyed on the MERGE-STABLE id (not player_id, which can go
        # NULL on a merge and silently break ON CONFLICT).
        UniqueConstraint(
            "gsis_id", "season_year", "scoring_format", "snapshot_label",
            name="uq_value_snapshot_gsis",
        ),
        # The ~8 players with no gsis_id fall back to name+position for the same guarantee.
        Index(
            "uq_value_snapshot_nogsis",
            "player_name", "position", "season_year", "scoring_format", "snapshot_label",
            unique=True,
            postgresql_where=text("gsis_id IS NULL"),
        ),
        # Outcome join (gsis) + merge-safe lookup.
        Index("ix_value_snapshot_join", "gsis_id", "season_year", "scoring_format"),
        Index("ix_value_snapshot_season_label", "season_year", "snapshot_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # ---- identity (merge-safe) ----
    season_year: Mapped[int] = mapped_column(Integer, nullable=False)      # season this value PREDICTS
    scoring_format: Mapped[str] = mapped_column(String(10), nullable=False)  # ppr | half_ppr | standard
    snapshot_label: Mapped[str] = mapped_column(String(40), nullable=False)  # e.g. "preseason_2026"
    player_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="SET NULL"), nullable=True,
    )
    gsis_id: Mapped[Optional[str]] = mapped_column(String(20), index=True)
    sportradar_id: Mapped[Optional[str]] = mapped_column(String(50))
    sleeper_id: Mapped[Optional[str]] = mapped_column(String(50))
    player_name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[Optional[str]] = mapped_column(String(5))

    # ---- our value (EFFECTIVE per-format value the board showed) ----
    projected_ppr: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 1))
    replacement_ppr: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 1))
    par_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 3))
    tier: Mapped[Optional[int]] = mapped_column(Integer)
    baseline_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    recommended_bid_ceiling: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    ceiling_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    floor_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    risk_adjusted_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    ai_bid_ceiling: Mapped[Optional[int]] = mapped_column(Integer)
    value_gap: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))         # PPR-basis (see note)
    value_gap_signal: Mapped[Optional[str]] = mapped_column(String(20))
    value_assessment: Mapped[Optional[str]] = mapped_column(String(20))
    pay_up_flag: Mapped[Optional[bool]] = mapped_column(Boolean)
    nomination_target_flag: Mapped[Optional[bool]] = mapped_column(Boolean)

    # ---- market side (at snapshot time) + provenance ----
    market_value_fantasypros: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    adp_fantasypros: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1))
    market_value_league: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    market_source: Mapped[Optional[str]] = mapped_column(String(60))
    market_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # ---- engine identity ----
    valuation_agent_version: Mapped[Optional[str]] = mapped_column(String(10))
    profiles_prompt_version: Mapped[Optional[str]] = mapped_column(String(10))
    git_sha: Mapped[Optional[str]] = mapped_column(String(12))
    pipeline_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # ---- OUTCOME side (written back after the season; the only permitted later UPDATE) ----
    actual_points: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 1))
    actual_games: Mapped[Optional[int]] = mapped_column(Integer)
    outcome_written_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
