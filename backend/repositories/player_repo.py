"""
PlayerRepository — all Player table queries.

Owns query construction (filters, sorting, pagination, eager loads)
so routers never touch SQLAlchemy directly.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.models.dependency import PlayerDependency
from backend.models.player import Player
from backend.repositories.base import BaseRepository

# Relationships needed to build a PlayerSummary response.
_SUMMARY_LOADS = (
    selectinload(Player.dependencies),
    selectinload(Player.injury_profile),
    selectinload(Player.schedule),
    selectinload(Player.historic_prices),
)

# Additional relationships for the full PlayerDetail response.
_DETAIL_LOADS = _SUMMARY_LOADS + (
    selectinload(Player.profile),
    selectinload(Player.beat_signals),
)

# Whitelist of sortable columns exposed by GET /players.
SORTABLE_COLUMNS = {
    "bid_ceiling": Player.recommended_bid_ceiling,
    "ai_ceiling": Player.ai_bid_ceiling,
    "system_value": Player.baseline_value,
    "market_value": Player.market_value,
    "value_gap": Player.value_gap,
    "name": Player.name,
    "tier": Player.tier,
}

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


class PlayerRepository(BaseRepository[Player]):
    """Read access to players and their pipeline-generated relations."""

    model = Player

    async def list_with_league_market_values(self) -> list[Player]:
        """Skill-position players that have a league market value set."""
        result = await self._session.execute(
            select(Player)
            .where(Player.market_value_league.isnot(None))
            .where(Player.position.in_(SKILL_POSITIONS))
        )
        return list(result.scalars().all())

    async def count_by_team(self) -> dict[str, int]:
        """Player counts keyed by team abbreviation."""
        result = await self._session.execute(
            select(Player.team_abbr, func.count(Player.id))
            .where(Player.team_abbr.isnot(None))
            .group_by(Player.team_abbr)
        )
        return dict(result.all())

    async def list_skill_players_for_team(self, team_abbr: str) -> list[Player]:
        """A team's skill-position players, best bid ceilings first."""
        result = await self._session.execute(
            select(Player)
            .where(Player.team_abbr == team_abbr)
            .where(Player.position.in_(SKILL_POSITIONS))
            .options(selectinload(Player.dependencies))
            .order_by(Player.recommended_bid_ceiling.desc().nulls_last())
        )
        return list(result.scalars().all())

    async def search_by_name(self, q: str, limit: int = 20) -> list[Player]:
        """Case-insensitive name search, best bid ceilings first."""
        result = await self._session.execute(
            select(Player)
            .where(Player.name.ilike(f"%{q}%"))
            .options(*_SUMMARY_LOADS)
            .order_by(Player.recommended_bid_ceiling.desc().nulls_last())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_detail(self, player_id: uuid.UUID) -> Player | None:
        """Fetch one player with every relation the detail view needs."""
        result = await self._session.execute(
            select(Player)
            .where(Player.id == player_id)
            .options(*_DETAIL_LOADS)
        )
        return result.scalar_one_or_none()

    async def count_by_position_tier(self) -> list[tuple[str, int | None, int]]:
        """(position, tier, count) rows for skill positions."""
        result = await self._session.execute(
            select(
                Player.position,
                Player.tier,
                func.count(Player.id),
            )
            .where(Player.position.in_(SKILL_POSITIONS))
            .group_by(Player.position, Player.tier)
        )
        return list(result.all())

    async def count_all(self) -> int:
        """Total number of player rows."""
        result = await self._session.execute(select(func.count(Player.id)))
        return result.scalar() or 0

    async def list_filtered(
        self,
        *,
        position: str | None = None,
        tier: int | None = None,
        team: str | None = None,
        flag: str | None = None,
        value_gap_dir: str | None = None,
        sort: str = "bid_ceiling",
        order: str = "desc",
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[Player], int]:
        """Filtered, sorted, paginated player list.

        Returns (players for the requested page, total matching count).
        Unknown sort keys fall back to bid_ceiling.
        """
        query = select(Player).options(*_SUMMARY_LOADS)

        if position:
            query = query.where(Player.position == position.upper())
        if tier is not None:
            query = query.where(Player.tier == tier)
        if team:
            query = query.where(Player.team_abbr == team.upper())
        if value_gap_dir == "undervalued":
            query = query.where(Player.value_gap_signal == "market_undervalues")
        elif value_gap_dir == "overvalued":
            query = query.where(Player.value_gap_signal == "market_overvalues")
        elif value_gap_dir == "aligned":
            query = query.where(Player.value_gap_signal == "aligned")

        if flag == "flagged":
            query = query.where(
                Player.id.in_(select(PlayerDependency.player_id).distinct())
            )
        elif flag == "clean":
            query = query.where(
                ~Player.id.in_(select(PlayerDependency.player_id).distinct())
            )

        count_result = await self._session.execute(
            select(func.count()).select_from(query.subquery())
        )
        total = count_result.scalar() or 0

        sort_col = SORTABLE_COLUMNS.get(sort, Player.recommended_bid_ceiling)
        if order == "asc":
            query = query.order_by(sort_col.asc().nulls_last())
        else:
            query = query.order_by(sort_col.desc().nulls_last())

        query = query.offset((page - 1) * per_page).limit(per_page)
        result = await self._session.execute(query)
        return list(result.scalars().all()), total
