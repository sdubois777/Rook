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
        player_position, player_injury_status).
        """
        query = (
            select(
                BeatReporterSignal,
                Player.name,
                Player.team_abbr,
                Player.position,
                Player.injury_status,
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

    async def distinct_signal_types(self) -> list[tuple[str, int]]:
        """The signal_type values actually present in the feed, most-common
        first. Powers the Type filter so its options are DERIVED from the data
        and can never drift from what the ingestion agent writes."""
        result = await self._session.execute(
            select(BeatReporterSignal.signal_type, func.count())
            .group_by(BeatReporterSignal.signal_type)
            .order_by(func.count().desc())
        )
        return [(row[0], row[1]) for row in result.all()]
