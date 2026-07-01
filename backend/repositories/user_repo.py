"""
UserRepository — all user-related DB queries.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select, update

from backend.models.user import User
from backend.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_external_id(
        self, external_id: str
    ) -> User | None:
        result = await self._session.execute(
            select(User).where(
                User.external_id == external_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_draft_token(
        self, draft_token: str
    ) -> User | None:
        result = await self._session.execute(
            select(User).where(
                User.draft_token == draft_token
            )
        )
        return result.scalar_one_or_none()

    async def get_by_email(
        self, email: str
    ) -> User | None:
        result = await self._session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    async def get_by_stripe_customer_id(
        self, customer_id: str
    ) -> User | None:
        """Resolve a user by their Stripe customer id.

        The Stripe webhook uses this exclusively — never a client-influenced
        payload field — to bind a verified event to the right user row.
        """
        result = await self._session.execute(
            select(User).where(
                User.stripe_customer_id == customer_id
            )
        )
        return result.scalar_one_or_none()

    async def set_stripe_customer_id(
        self, user_id: uuid.UUID, customer_id: str
    ) -> None:
        """Persist the Stripe customer id (set at checkout creation). No commit."""
        await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(stripe_customer_id=customer_id)
        )

    async def set_stripe_subscription_id(
        self, user_id: uuid.UUID, subscription_id: str | None
    ) -> None:
        """Set or clear (None) the Stripe subscription id. No commit."""
        await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(stripe_subscription_id=subscription_id)
        )

    async def set_subscription_status(
        self, user_id: uuid.UUID, status: str | None
    ) -> None:
        """Set billing status ('active'|'past_due'|'canceling'|None). No commit."""
        await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(subscription_status=status)
        )

    async def rotate_draft_token(self, user_id: uuid.UUID) -> str:
        """Assign a fresh draft token to the user and commit.

        Used both to mint a first token and to revoke-and-replace an
        existing one (the old token stops authenticating immediately).
        """
        user = await self.get_or_404(user_id)
        user.draft_token = str(uuid.uuid4())
        await self._session.commit()
        return user.draft_token

    async def update_credits(
        self,
        user_id: uuid.UUID,
        delta: int,
    ) -> int:
        """
        Atomically update credits by delta.
        Returns new balance.
        Negative delta = deduct.
        Enforces floor of 0.
        """
        result = await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .where(User.credits_remaining >= -delta)
            # Prevents going below 0
            .values(
                credits_remaining=(
                    User.credits_remaining + delta
                )
            )
            .returning(User.credits_remaining)
        )
        row = result.fetchone()
        if row is None:
            # Where clause failed — insufficient credits
            user = await self.get_or_404(user_id)
            return user.credits_remaining
            # Return current balance, caller handles error
        return row[0]

    async def update_tier(
        self,
        user_id: uuid.UUID,
        tier: str,
        credits_bonus: int = 0,
    ) -> User:
        """Upgrade/downgrade tier and optionally add bonus credits."""
        await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                tier=tier,
                credits_remaining=(
                    User.credits_remaining + credits_bonus
                ),
            )
        )
        return await self.get_or_404(user_id)

    async def add_monthly_credits(
        self,
        tier: str,
        monthly_amount: int,
    ) -> int:
        """
        Add monthly credits for all users of a tier.
        Returns count of users updated.
        Credits accumulate — never reset to cap.
        """
        result = await self._session.execute(
            update(User)
            .where(User.tier == tier)
            .where(User.deleted_at.is_(None))
            .values(
                credits_remaining=(
                    User.credits_remaining + monthly_amount
                )
            )
            .returning(User.id)
        )
        return len(result.fetchall())
