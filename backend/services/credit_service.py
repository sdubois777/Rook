"""
CreditService — credit checking and deduction.

All credit operations go through here.
Transactional: check → deduct → log in one operation.
Never deduct without logging. Never log without deducting.
"""
from __future__ import annotations

from backend.core.exceptions import InsufficientCreditsError
from backend.models.user import CREDIT_COSTS, User
from backend.repositories.credit_repo import CreditRepository
from backend.repositories.user_repo import UserRepository


class CreditService:
    def __init__(
        self,
        user_repo: UserRepository,
        credit_repo: CreditRepository,
    ):
        self._user_repo = user_repo
        self._credit_repo = credit_repo

    async def deduct(
        self,
        user: User,
        action: str,
        agent_name: str | None = None,
        cost_usd: float | None = None,
    ) -> int:
        """
        Deduct credits for an action.
        Returns new credit balance.
        Raises InsufficientCreditsError if too low.

        Transaction is atomic:
          1. Check balance
          2. Deduct (enforced at DB level)
          3. Log usage
        All three succeed or none do.
        """
        cost = CREDIT_COSTS.get(action, 0)
        if cost == 0:
            return user.credits_remaining
            # Free action — no deduction needed

        if user.credits_remaining < cost:
            raise InsufficientCreditsError(
                required=cost,
                available=user.credits_remaining,
            )

        # Atomic deduction — DB enforces no negative balance
        new_balance = await self._user_repo.update_credits(
            user.id, delta=-cost
        )

        # Log the transaction
        await self._credit_repo.log_usage(
            user_id=user.id,
            action=action,
            credits_used=cost,
            agent_name=agent_name,
            cost_usd=cost_usd,
        )

        await self._user_repo.commit()
        return new_balance

    async def get_balance(self, user: User) -> int:
        """Current credit balance (live from DB)."""
        refreshed = await self._user_repo.get_or_404(
            user.id
        )
        return refreshed.credits_remaining

    async def get_usage_history(
        self, user: User, days: int = 30
    ) -> list:
        return await self._credit_repo.get_usage_history(
            user.id, days=days
        )
