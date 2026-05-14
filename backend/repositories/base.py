"""
BaseRepository — generic CRUD operations.

All repositories extend this.
No raw SQL. No session management.
Never open sessions here — they're injected.

Usage:
    class PlayerRepository(BaseRepository[Player]):
        model = Player

        async def find_by_tier(self, tier: int):
            result = await self._session.execute(
                select(self.model).where(
                    self.model.tier == tier
                )
            )
            return result.scalars().all()
"""
from __future__ import annotations

import uuid
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import NotFoundError
from backend.database import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    model: type[T]

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, id: uuid.UUID) -> T | None:
        return await self._session.get(self.model, id)

    async def get_or_404(self, id: uuid.UUID) -> T:
        obj = await self.get(id)
        if obj is None:
            name = self.model.__name__
            raise NotFoundError(f"{name} {id} not found")
        return obj

    async def list_all(self) -> list[T]:
        result = await self._session.execute(
            select(self.model)
        )
        return list(result.scalars().all())

    async def create(self, **kwargs) -> T:
        obj = self.model(**kwargs)
        self._session.add(obj)
        await self._session.flush()  # get ID without commit
        return obj

    async def delete(self, obj: T) -> None:
        await self._session.delete(obj)
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()

    async def refresh(self, obj: T) -> T:
        await self._session.refresh(obj)
        return obj
