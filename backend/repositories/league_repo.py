"""
LeagueRepository — user league CRUD.
All queries automatically scoped to user_id.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, func, update
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
        """Cap denominator: current-season (is_active) AND non-suspended."""
        result = await self._session.execute(
            select(func.count(UserLeague.id))
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.is_active.is_(True))
            .where(UserLeague.suspended_at.is_(None))
        )
        return result.scalar() or 0

    async def get_active_leagues(
        self, user_id: uuid.UUID
    ) -> list[UserLeague]:
        """The usable set — current-season, non-suspended — ordered oldest first
        (deterministic for chooser candidates + restore ordering)."""
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.is_active.is_(True))
            .where(UserLeague.suspended_at.is_(None))
            .order_by(UserLeague.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_current_season_leagues(
        self, user_id: uuid.UUID
    ) -> list[UserLeague]:
        """All current-season leagues (active + suspended), oldest first — the
        chooser's candidate set (finished/past-season leagues excluded)."""
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.is_active.is_(True))
            .order_by(UserLeague.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_suspended_leagues(
        self, user_id: uuid.UUID
    ) -> list[UserLeague]:
        """Parked current-season leagues, longest-parked first (restore order)."""
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.is_active.is_(True))
            .where(UserLeague.suspended_at.is_not(None))
            .order_by(UserLeague.suspended_at.asc())
        )
        return list(result.scalars().all())

    async def set_suspended(
        self,
        user_id: uuid.UUID,
        league_ids: list[uuid.UUID],
        suspended_at: datetime | None,
    ) -> None:
        """Park (suspended_at=now) or restore (None) specific leagues, scoped to
        the user. No commit — caller owns the transaction."""
        if not league_ids:
            return
        await self._session.execute(
            update(UserLeague)
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.id.in_(league_ids))
            .values(suspended_at=suspended_at)
        )

    async def get_user_leagues_by_platform(
        self,
        user_id: uuid.UUID,
        platform: str,
    ) -> list[UserLeague]:
        """Usable leagues for a platform — active, non-suspended (passive sync
        must not touch parked leagues)."""
        result = await self._session.execute(
            select(UserLeague)
            .where(UserLeague.user_id == user_id)
            .where(UserLeague.platform == platform)
            .where(UserLeague.is_active.is_(True))
            .where(UserLeague.suspended_at.is_(None))
        )
        return list(result.scalars().all())

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
