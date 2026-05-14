"""
LeagueRepository — user league CRUD.
All queries automatically scoped to user_id.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select, func

from backend.models.user_league import UserLeague
from backend.repositories.base import BaseRepository


class LeagueRepository(BaseRepository[UserLeague]):
    model = UserLeague

    async def get_by_user(
        self, user_id: uuid.UUID
    ) -> list[UserLeague]:
        """Always scoped to user — never returns another user's leagues."""
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.is_active.is_(True))
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

    async def count_active(
        self, user_id: uuid.UUID
    ) -> int:
        result = await self._session.execute(
            select(func.count(UserLeague.id))
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.is_active.is_(True))
        )
        return result.scalar() or 0
