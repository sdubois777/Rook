"""
UserLeague — a user's fantasy league configuration.

One record per user per league per season.
Stores LeagueConfig parameters persistently.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB, UUID

from backend.database import Base


class UserLeague(Base):
    __tablename__ = "user_leagues"
    __table_args__ = (
        # One league config per user/platform/league/season
        {"extend_existing": True},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )

    # Platform league identity
    platform: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    # "yahoo" | "espn" | "sleeper"
    league_id: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    league_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )

    # LeagueConfig parameters
    team_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12
    )
    draft_type: Mapped[str] = mapped_column(
        String(10), nullable=False, default="auction"
    )
    # "auction" | "snake"
    scoring: Mapped[str] = mapped_column(
        String(10), nullable=False, default="ppr"
    )
    # "ppr" | "half_ppr" | "standard"
    budget: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    # auction only — null for snake
    season_year: Mapped[int] = mapped_column(
        Integer, nullable=False
    )

    # Manager name mapping {platform_team_id: manager_name}
    manager_map: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    last_synced: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
