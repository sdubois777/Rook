"""
CreditRepository — credit ledger operations.
All credit transactions are logged here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func

from backend.models.user import CreditUsageLog
from backend.repositories.base import BaseRepository


class CreditRepository(BaseRepository[CreditUsageLog]):
    model = CreditUsageLog

    async def log_usage(
        self,
        user_id: uuid.UUID,
        action: str,
        credits_used: int,
        agent_name: str | None = None,
        cost_usd: float | None = None,
    ) -> CreditUsageLog:
        return await self.create(
            user_id=user_id,
            action=action,
            credits_used=credits_used,
            agent_name=agent_name,
            cost_usd=cost_usd,
        )

    async def get_usage_history(
        self,
        user_id: uuid.UUID,
        days: int = 30,
        limit: int = 50,
    ) -> list[CreditUsageLog]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await self._session.execute(
            select(CreditUsageLog)
            .where(CreditUsageLog.user_id == user_id)
            .where(CreditUsageLog.created_at >= cutoff)
            .order_by(CreditUsageLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_total_used(
        self,
        user_id: uuid.UUID,
        days: int = 30,
    ) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await self._session.execute(
            select(func.coalesce(
                func.sum(CreditUsageLog.credits_used), 0
            ))
            .where(CreditUsageLog.user_id == user_id)
            .where(CreditUsageLog.created_at >= cutoff)
        )
        return result.scalar() or 0
