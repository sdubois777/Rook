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

        Features: live_draft, trade_analyzer,
                  trade_finder, waiver_wire
        injury_monitoring is always True (all tiers).
        """
        limits = TIER_LIMITS.get(user.tier, {})
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
    tier_order = ["intro", "standard", "pro"]
    for tier in tier_order:
        limits = TIER_LIMITS.get(tier, {})
        if limits.get(feature, False):
            return tier
    return "pro"  # fallback
