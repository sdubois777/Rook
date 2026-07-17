"""
PlayerFormatValues — per-scoring-format valuation rows.

One row per (player, scoring_format). The PPR row mirrors the values on the players
table (kept authoritative there for back-compat); Half/Standard rows are the repriced
values. All three derive from ONE scoring definition (backend.scoring) applied to the
same shared analysis — the read layer selects the row for the league's format.

Additive, nullable — no rewrite of existing data. Value/prose columns are added here
as each pipeline stage lands (this table starts with the scoring-repriced VALUE set;
per-format ADP/auction/prose columns follow in later slices).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class PlayerFormatValues(Base):
    __tablename__ = "player_format_values"
    __table_args__ = (
        UniqueConstraint("player_id", "scoring_format", name="uq_player_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    player_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scoring_format: Mapped[str] = mapped_column(String(10), nullable=False)  # ppr | half_ppr | standard

    # Scoring-repriced VALUE set (same PAR/VOR math as the PPR pass, different points).
    projected_points: Mapped[float | None] = mapped_column(Numeric(6, 1))
    replacement_ppr: Mapped[float | None] = mapped_column(Numeric(6, 1))
    tier: Mapped[int | None] = mapped_column(Integer)
    baseline_value: Mapped[float | None] = mapped_column(Numeric(5, 2))
    recommended_bid_ceiling: Mapped[float | None] = mapped_column(Numeric(5, 2))
    ceiling_value: Mapped[float | None] = mapped_column(Numeric(5, 2))
    floor_value: Mapped[float | None] = mapped_column(Numeric(5, 2))
    risk_adjusted_value: Mapped[float | None] = mapped_column(Numeric(5, 2))

    # Per-format NARRATIVE (G2). The PPR row copies the players-table prose (byte-
    # identical for existing users); Half/Standard carry format-appropriate prose
    # that must not sell reception-dependent players on catches/targets.
    value_assessment: Mapped[str | None] = mapped_column(String(20))
    auction_note: Mapped[str | None] = mapped_column(Text)

    # Per-format HYBRID auction $ — the valuation agent's independent, market-blind
    # opinion (tier-band anchor ±25%, reasoned over football signals). Half/Standard only;
    # PPR keeps ai_bid_ceiling on the players table. Read by format_display.overlay_for →
    # the draft board / players / detail surfaces (the non-PPR headline auction $).
    ai_bid_ceiling: Mapped[int | None] = mapped_column(Integer)

    # Per-format MARKET INPUTS (G5), re-scraped every pipeline run. ADP = FantasyPros
    # overall rank (same integer scale as players.adp_fantasypros). auction_value =
    # DraftWizard $ for the CANONICAL 12-team/1-flex roster (auction_roster_shape
    # records that shape so Phase 2 can disclose it to non-canonical leagues).
    # These are INERT until a Phase 2 read surface threads formats in.
    adp_fantasypros: Mapped[float | None] = mapped_column(Numeric(5, 1))
    auction_value: Mapped[float | None] = mapped_column(Numeric(5, 2))
    auction_roster_shape: Mapped[str | None] = mapped_column(String(40))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
