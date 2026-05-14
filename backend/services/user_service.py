"""
UserService — user account management.

Business logic only. No HTTP concerns.
No imports from fastapi.
"""
from __future__ import annotations

from backend.models.user import TIER_LIMITS, User
from backend.repositories.user_repo import UserRepository


class UserService:
    def __init__(self, repo: UserRepository):
        self._repo = repo

    async def get_or_create(
        self,
        external_id: str,
        email: str,
        display_name: str = "",
    ) -> tuple[User, bool]:
        """
        Get existing user or create new one.
        Returns (user, created) — created=True if new.
        Intro users get signup bonus (25 credits) immediately.
        Paid tier bonuses applied via Stripe webhook (Stage 26).
        """
        user = await self._repo.get_by_external_id(
            external_id
        )
        if user:
            # Update email if we now have a real one
            if email and "@placeholder." not in email and user.email != email:
                user.email = email
                await self._repo.commit()
            return user, False

        # Check if email already exists (e.g. dev stub user)
        existing = await self._repo.get_by_email(email)
        if existing:
            # Adopt the existing record — link it to the real Clerk ID
            existing.external_id = external_id
            if display_name:
                existing.display_name = display_name
            await self._repo.commit()
            return existing, False

        initial_tier = "intro"
        signup_bonus = TIER_LIMITS[initial_tier].get(
            "credits_signup_bonus", 0
        )
        user = await self._repo.create(
            external_id=external_id,
            email=email,
            display_name=display_name,
            tier=initial_tier,
            credits_remaining=signup_bonus,
        )
        await self._repo.commit()
        return user, True

    async def apply_signup_bonus(
        self, user: User
    ) -> User:
        """
        Apply one-time signup credits for tier.
        Called by Stripe webhook on first payment.
        """
        bonus = TIER_LIMITS.get(
            user.tier, {}
        ).get("credits_signup_bonus", 0)

        if bonus > 0:
            await self._repo.update_credits(
                user.id, delta=bonus
            )
            await self._repo.commit()

        return await self._repo.get_or_404(user.id)

    async def upgrade_tier(
        self,
        user: User,
        new_tier: str,
    ) -> User:
        """Change user tier. Adds signup bonus for new tier."""
        if new_tier not in TIER_LIMITS:
            from backend.core.exceptions import ValidationError
            raise ValidationError(
                f"Invalid tier: {new_tier}. "
                f"Must be: {list(TIER_LIMITS.keys())}"
            )

        bonus = TIER_LIMITS[new_tier].get(
            "credits_signup_bonus", 0
        )
        updated = await self._repo.update_tier(
            user.id,
            tier=new_tier,
            credits_bonus=bonus,
        )
        await self._repo.commit()
        return updated
