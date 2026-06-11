"""
LeagueAuctionHistoryRepository — user-scoped queries over imported
auction draft history.

Every method filters by (user_id, user_league_id) so one user's league
data can never leak into another's analysis.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select

from backend.models.league_auction_history import LeagueAuctionHistory
from backend.repositories.base import BaseRepository

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

# Trend aggregates ignore $1 throwaway picks; manager-tendency
# aggregates ignore anything at or under $3 (end-of-draft filler).
_MIN_TREND_PRICE = 1
_MIN_TENDENCY_PRICE = 3


class LeagueAuctionHistoryRepository(BaseRepository[LeagueAuctionHistory]):
    """Read/delete access to league_auction_history rows."""

    model = LeagueAuctionHistory

    def _scope(self, user_id: uuid.UUID, league_id: uuid.UUID) -> list[Any]:
        """WHERE clauses limiting rows to one user's league."""
        return [
            LeagueAuctionHistory.user_id == user_id,
            LeagueAuctionHistory.user_league_id == league_id,
        ]

    async def list_seasons(
        self, user_id: uuid.UUID, league_id: uuid.UUID
    ) -> list[int]:
        """Distinct seasons with imported history, ascending."""
        result = await self._session.execute(
            select(LeagueAuctionHistory.season_year)
            .where(*self._scope(user_id, league_id))
            .distinct()
            .order_by(LeagueAuctionHistory.season_year)
        )
        return [row[0] for row in result.all()]

    async def position_trends(
        self, user_id: uuid.UUID, league_id: uuid.UUID
    ) -> list[Any]:
        """Per-season, per-position price aggregates.

        Rows expose season_year, position, avg_price, max_price,
        total_spent, player_count.
        """
        result = await self._session.execute(
            select(
                LeagueAuctionHistory.season_year,
                LeagueAuctionHistory.position,
                func.avg(LeagueAuctionHistory.price).label("avg_price"),
                func.max(LeagueAuctionHistory.price).label("max_price"),
                func.sum(LeagueAuctionHistory.price).label("total_spent"),
                func.count().label("player_count"),
            )
            .where(
                *self._scope(user_id, league_id),
                LeagueAuctionHistory.price > _MIN_TREND_PRICE,
                LeagueAuctionHistory.position.in_(SKILL_POSITIONS),
            )
            .group_by(
                LeagueAuctionHistory.season_year,
                LeagueAuctionHistory.position,
            )
            .order_by(
                LeagueAuctionHistory.season_year,
                LeagueAuctionHistory.position,
            )
        )
        return list(result.all())

    async def manager_tendencies(
        self, user_id: uuid.UUID, league_id: uuid.UUID
    ) -> list[Any]:
        """Per-manager, per-position spend aggregates.

        Rows expose manager_name, position, avg_spend, total_spend, picks.
        """
        result = await self._session.execute(
            select(
                LeagueAuctionHistory.manager_name,
                LeagueAuctionHistory.position,
                func.avg(LeagueAuctionHistory.price).label("avg_spend"),
                func.sum(LeagueAuctionHistory.price).label("total_spend"),
                func.count().label("picks"),
            )
            .where(
                *self._scope(user_id, league_id),
                LeagueAuctionHistory.price > _MIN_TENDENCY_PRICE,
                LeagueAuctionHistory.manager_name.isnot(None),
                LeagueAuctionHistory.manager_name != "",
                LeagueAuctionHistory.position.in_(SKILL_POSITIONS),
            )
            .group_by(
                LeagueAuctionHistory.manager_name,
                LeagueAuctionHistory.position,
            )
            .order_by(
                LeagueAuctionHistory.manager_name,
                func.avg(LeagueAuctionHistory.price).desc(),
            )
        )
        return list(result.all())

    async def season_summaries(
        self, user_id: uuid.UUID, league_id: uuid.UUID
    ) -> list[Any]:
        """Per-season pick counts and total spend, newest first.

        Rows expose season_year, pick_count, total_spent, source.
        """
        result = await self._session.execute(
            select(
                LeagueAuctionHistory.season_year,
                func.count().label("pick_count"),
                func.sum(LeagueAuctionHistory.price).label("total_spent"),
                LeagueAuctionHistory.source,
            )
            .where(*self._scope(user_id, league_id))
            .group_by(
                LeagueAuctionHistory.season_year,
                LeagueAuctionHistory.source,
            )
            .order_by(LeagueAuctionHistory.season_year.desc())
        )
        return list(result.all())

    async def list_picks(
        self, user_id: uuid.UUID, league_id: uuid.UUID, season: int
    ) -> list[LeagueAuctionHistory]:
        """All picks for one season, in draft order."""
        result = await self._session.execute(
            select(LeagueAuctionHistory)
            .where(
                *self._scope(user_id, league_id),
                LeagueAuctionHistory.season_year == season,
            )
            .order_by(
                LeagueAuctionHistory.draft_pick_number.asc().nulls_last()
            )
        )
        return list(result.scalars().all())

    async def delete_for_league(
        self, user_id: uuid.UUID, league_id: uuid.UUID
    ) -> None:
        """Delete every history row for one user's league. No commit."""
        await self._session.execute(
            delete(LeagueAuctionHistory).where(
                *self._scope(user_id, league_id)
            )
        )
