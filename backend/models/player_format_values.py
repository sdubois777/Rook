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

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
