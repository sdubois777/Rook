"""
TeamSystemRepository — queries over per-season team system grades.

The team_systems table holds one row per (team, season); consumers
almost always want the latest season per team.
"""
from __future__ import annotations

from sqlalchemy import func, select

from backend.models.team_system import TeamSystem
from backend.repositories.base import BaseRepository


class TeamSystemRepository(BaseRepository[TeamSystem]):
    """Read access to team_systems rows."""

    model = TeamSystem

    async def list_latest(self) -> list[TeamSystem]:
        """The most recent season's row for every team."""
        subq = (
            select(
                TeamSystem.team_abbr,
                func.max(TeamSystem.season_year).label("max_year"),
            )
            .group_by(TeamSystem.team_abbr)
            .subquery()
        )
        result = await self._session.execute(
            select(TeamSystem)
            .join(
                subq,
                (TeamSystem.team_abbr == subq.c.team_abbr)
                & (TeamSystem.season_year == subq.c.max_year),
            )
        )
        return list(result.scalars().all())

    async def get_latest_for_team(self, team_abbr: str) -> TeamSystem | None:
        """The most recent season's row for one team."""
        result = await self._session.execute(
            select(TeamSystem)
            .where(TeamSystem.team_abbr == team_abbr)
            .order_by(TeamSystem.season_year.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
