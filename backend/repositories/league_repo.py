"""
LeagueRepository — user league CRUD.
All queries automatically scoped to user_id.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from backend.models.user_league import UserLeague
from backend.repositories.base import BaseRepository


class LeagueRepository(BaseRepository[UserLeague]):
    model = UserLeague

    async def get_by_user(
        self, user_id: uuid.UUID
    ) -> list[UserLeague]:
        """All leagues for user — active and finished.

        is_active is used by in-season features (lineup optimizer,
        waiver wire) but NOT for display. Users need to see history.
        """
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.user_id == user_id)
            .order_by(UserLeague.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_user_league(
        self,
        user_id: uuid.UUID,
        league_id: uuid.UUID,
    ) -> UserLeague | None:
        """Get a specific league, enforcing user ownership."""
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.id == league_id)
            .where(UserLeague.user_id == user_id)
            # user_id check = row-level security
        )
        return result.scalar_one_or_none()

    async def find_by_identity(
        self,
        user_id: uuid.UUID,
        platform: str,
        league_id: str,
    ) -> UserLeague | None:
        """Find by (user_id, platform, league_id) — unique constraint key."""
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.platform == platform)
            .where(UserLeague.league_id == league_id)
        )
        return result.scalar_one_or_none()

    async def count_active(
        self, user_id: uuid.UUID
    ) -> int:
        result = await self._session.execute(
            select(func.count(UserLeague.id))
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.is_active.is_(True))
        )
        return result.scalar() or 0

    async def upsert(
        self,
        user_id: uuid.UUID,
        platform: str,
        league_id: str,
        *,
        season_year: int,
        team_count: int = 12,
        draft_type: str = "auction",
        scoring: str = "ppr",
        budget: int | None = 200,
        is_active: bool = True,
    ) -> UserLeague:
        """Insert or update a league, keyed on (user_id, platform, league_id)."""
        stmt = (
            pg_insert(UserLeague)
            .values(
                user_id=user_id,
                platform=platform,
                league_id=league_id,
                season_year=season_year,
                team_count=team_count,
                draft_type=draft_type,
                scoring=scoring,
                budget=budget,
                is_active=is_active,
            )
            .on_conflict_do_update(
                constraint="uq_user_leagues_user_platform_league",
                set_={
                    "season_year": season_year,
                    "team_count": team_count,
                    "draft_type": draft_type,
                    "scoring": scoring,
                    "budget": budget,
                    "is_active": is_active,
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            .returning(UserLeague)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return result.scalar_one()
