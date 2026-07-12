"""
FeatureService — tier-based access control.

Stateless — all methods are class methods.
No DB access needed — tier data is in memory.
"""
from backend.core.exceptions import FeatureNotAvailableError
from backend.models.user import TIER_LIMITS, User


class FeatureService:

    @staticmethod
    def check_feature_access(
        user: User, feature: str
    ) -> None:
        """
        Raises FeatureNotAvailableError if user's tier
        doesn't include this feature.

        ENTITLEMENT features ONLY (binary 403 gates): live_draft,
        cross_league_view. Metered features (trade/waiver/finder) are NOT
        tier-gated -- everyone can use them; the free tier pays credits via
        CreditService.charge_metered. injury_monitoring is True on all tiers.

        Uses the EFFECTIVE tier -- an expired season entitlement gates as free.
        """
        from backend.models.user import effective_tier

        limits = TIER_LIMITS.get(effective_tier(user), {})
        has_access = limits.get(feature, False)

        if not has_access:
            # Find minimum tier that includes this feature
            required = _find_min_tier(feature)
            raise FeatureNotAvailableError(
                feature=feature,
                required_tier=required,
            )

    @staticmethod
    def can_add_league(user: User, current_count: int) -> None:
        """Raises LeagueLimitError if user is at their league limit."""
        from backend.core.exceptions import LeagueLimitError

        limits = TIER_LIMITS.get(user.tier, {})
        max_leagues = limits.get("max_leagues")

        if max_leagues is not None and current_count >= max_leagues:
            raise LeagueLimitError(
                current=current_count,
                max_leagues=max_leagues,
            )

    @staticmethod
    def get_limits(user: User) -> dict:
        """Return full tier limits for user's tier."""
        return TIER_LIMITS.get(user.tier, {}).copy()


def _find_min_tier(feature: str) -> str:
    """Find minimum tier that includes a feature."""
    from backend.models.user import TIER_ORDER
    tier_order = list(TIER_ORDER)
    for tier in tier_order:
        limits = TIER_LIMITS.get(tier, {})
        if limits.get(feature, False):
            return tier
    return "pro"  # fallback
