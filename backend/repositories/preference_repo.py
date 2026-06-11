"""
UserPreferenceRepository — watchlist and strategy preference rows.

Preferences are keyed by (user_id, preference_type, entity_id):
watchlist rows carry a player id in entity_id; the strategy row is a
singleton per user with the choice stored in the value JSON.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from backend.models.user_preference import UserPreference
from backend.repositories.base import BaseRepository

WATCHLIST = "watchlist"
STRATEGY = "strategy"


class UserPreferenceRepository(BaseRepository[UserPreference]):
    """CRUD access to user_preferences rows."""

    model = UserPreference

    async def list_watchlist(self, user_id: uuid.UUID) -> list[UserPreference]:
        """All watchlist rows for a user, newest first."""
        result = await self._session.execute(
            select(UserPreference)
            .where(UserPreference.preference_type == WATCHLIST)
            .where(UserPreference.user_id == user_id)
            .order_by(UserPreference.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_watchlist_entry(
        self, user_id: uuid.UUID, player_id: str
    ) -> UserPreference | None:
        """The watchlist row for one player, if present."""
        result = await self._session.execute(
            select(UserPreference)
            .where(UserPreference.preference_type == WATCHLIST)
            .where(UserPreference.entity_id == player_id)
            .where(UserPreference.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def add_watchlist_entry(
        self, user_id: uuid.UUID, player_id: str
    ) -> UserPreference:
        """Insert a watchlist row and commit."""
        pref = UserPreference(
            preference_type=WATCHLIST,
            entity_id=player_id,
            user_id=user_id,
            value={},
        )
        self._session.add(pref)
        await self._session.commit()
        await self._session.refresh(pref)
        return pref

    async def remove_watchlist_entry(
        self, user_id: uuid.UUID, player_id: str
    ) -> int:
        """Delete a watchlist row; returns the number of rows removed."""
        result = await self._session.execute(
            delete(UserPreference)
            .where(UserPreference.preference_type == WATCHLIST)
            .where(UserPreference.entity_id == player_id)
            .where(UserPreference.user_id == user_id)
        )
        await self._session.commit()
        return result.rowcount

    async def get_strategy(self, user_id: uuid.UUID) -> UserPreference | None:
        """The user's strategy row, if one exists."""
        result = await self._session.execute(
            select(UserPreference)
            .where(UserPreference.preference_type == STRATEGY)
            .where(UserPreference.user_id == user_id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def set_strategy(self, user_id: uuid.UUID, strategy: str) -> None:
        """Upsert the user's strategy row and commit."""
        pref = await self.get_strategy(user_id)
        if pref:
            pref.value = {"strategy": strategy}
        else:
            pref = UserPreference(
                preference_type=STRATEGY,
                entity_id=None,
                user_id=user_id,
                value={"strategy": strategy},
            )
            self._session.add(pref)
        await self._session.commit()
