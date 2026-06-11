"""
NewsRepository — beat reporter signal feed queries.

Signals join to players so the feed can show name/team/position next
to each item; the join is an outer join because some signals are not
linked to a player.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from backend.models.dependency import BeatReporterSignal
from backend.models.player import Player
from backend.repositories.base import BaseRepository


class NewsRepository(BaseRepository[BeatReporterSignal]):
    """Read access to beat_reporter_signals rows."""

    model = BeatReporterSignal

    async def list_feed(
        self,
        *,
        cutoff: datetime,
        team: str | None = None,
        player_id: uuid.UUID | None = None,
        signal_type: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[Any], int]:
        """Filtered, newest-first signal feed with player context.

        Returns (rows for the requested page, total matching count).
        Each row is (BeatReporterSignal, player_name, player_team,
        player_position).
        """
        query = (
            select(
                BeatReporterSignal,
                Player.name,
                Player.team_abbr,
                Player.position,
            )
            .outerjoin(Player, BeatReporterSignal.player_id == Player.id)
            .where(BeatReporterSignal.flagged_at >= cutoff)
        )

        if team:
            query = query.where(Player.team_abbr == team.upper())
        if player_id:
            query = query.where(BeatReporterSignal.player_id == player_id)
        if signal_type:
            query = query.where(BeatReporterSignal.signal_type == signal_type)

        count_result = await self._session.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = count_result.scalar() or 0

        query = (
            query.order_by(BeatReporterSignal.flagged_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        result = await self._session.execute(query)
        return list(result.all()), total
