"""
LeagueService — user league management.
All league operations are user-scoped.
"""
from __future__ import annotations

import uuid

from backend.core.exceptions import NotFoundError
from backend.models.user_league import UserLeague
from backend.repositories.league_repo import LeagueRepository


class LeagueService:
    def __init__(self, repo: LeagueRepository):
        self._repo = repo

    async def get_user_leagues(
        self, user_id: uuid.UUID
    ) -> list[UserLeague]:
        return await self._repo.get_by_user(user_id)

    async def add_league(
        self,
        user_id: uuid.UUID,
        platform: str,
        league_id: str,
        team_count: int,
        draft_type: str,
        scoring: str,
        budget: int | None,
        season_year: int,
    ) -> UserLeague:
        league = await self._repo.create(
            user_id=user_id,
            platform=platform,
            league_id=league_id,
            team_count=team_count,
            draft_type=draft_type,
            scoring=scoring,
            budget=budget,
            season_year=season_year,
            is_active=True,
        )
        await self._repo.commit()
        return league

    async def remove_league(
        self,
        user_id: uuid.UUID,
        league_id: uuid.UUID,
    ) -> None:
        league = await self._repo.get_user_league(
            user_id, league_id
        )
        if not league:
            raise NotFoundError(
                f"League {league_id} not found"
            )
        # Soft delete
        league.is_active = False
        await self._repo.commit()

    async def get_league_config(
        self,
        user_id: uuid.UUID,
        league_id: uuid.UUID,
    ):
        """
        Get LeagueConfig dataclass for a user's league.
        Used by valuation engine and agents.
        """
        from backend.models.league_config import LeagueConfig

        league = await self._repo.get_user_league(
            user_id, league_id
        )
        if not league:
            raise NotFoundError(
                f"League {league_id} not found"
            )

        return LeagueConfig(
            team_count=league.team_count,
            draft_type=league.draft_type,
            scoring=league.scoring,
            budget=league.budget or 200,
            platform=league.platform,
            league_id=league.league_id,
            season_year=league.season_year,
        )
