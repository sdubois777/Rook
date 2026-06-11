"""
LeagueService — user league management.
All league operations are user-scoped.
"""
from __future__ import annotations

import logging
import uuid

from backend.core.exceptions import NotFoundError
from backend.models.user_league import UserLeague
from backend.repositories.league_auction_repo import (
    LeagueAuctionHistoryRepository,
)
from backend.repositories.league_repo import LeagueRepository

logger = logging.getLogger(__name__)


class LeagueService:
    """User-scoped league CRUD orchestration."""

    def __init__(
        self,
        repo: LeagueRepository,
        history_repo: LeagueAuctionHistoryRepository,
    ):
        self._repo = repo
        self._history_repo = history_repo

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
        is_active: bool = True,
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
            is_active=is_active,
        )
        await self._repo.commit()
        return league

    async def delete_league(
        self,
        user_id: uuid.UUID,
        league_id: uuid.UUID,
    ) -> None:
        """Hard delete league and all child data."""
        league = await self._repo.get_user_league(
            user_id, league_id
        )
        if not league:
            raise NotFoundError(
                f"League {league_id} not found"
            )

        # Delete child data explicitly, then the league row
        await self._history_repo.delete_for_league(user_id, league_id)
        await self._repo.delete(league)
        await self._repo.commit()

    async def get_league_config(
        self,
        user_id: uuid.UUID,
        league_id: uuid.UUID | None,
    ):
        """
        Get LeagueConfig dataclass for a user's league.
        Used by valuation engine and agents.
        Falls back to DEFAULT_LEAGUE_CONFIG if league_id
        is None or the league doesn't exist.
        """
        from backend.models.league_config import (
            LeagueConfig,
            DEFAULT_LEAGUE_CONFIG,
        )

        if league_id is None:
            logger.info(
                "No league_id provided — "
                "using DEFAULT_LEAGUE_CONFIG"
            )
            return DEFAULT_LEAGUE_CONFIG

        league = await self._repo.get_user_league(
            user_id, league_id
        )
        if not league:
            logger.warning(
                "League %s not found for user %s "
                "— using DEFAULT_LEAGUE_CONFIG",
                league_id, user_id,
            )
            return DEFAULT_LEAGUE_CONFIG

        return LeagueConfig(
            team_count=league.team_count or 12,
            draft_type=league.draft_type or "auction",
            scoring=league.scoring or "ppr",
            budget=league.budget or 200,
            platform=league.platform,
            league_id=league.league_id,
            season_year=league.season_year,
        )
