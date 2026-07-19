"""Tests for FeatureService — tier-based access control."""
import pytest

from backend.core.exceptions import FeatureNotAvailableError, LeagueLimitError
from backend.services.feature_service import FeatureService


class _FakeUser:
    """Minimal user object for testing."""
    def __init__(self, tier: str):
        self.tier = tier
        self.tier_expires_at = None


def test_free_cannot_access_live_draft():
    """Entitlement gates remain binary: live draft is standard+."""
    user = _FakeUser("free")
    with pytest.raises(FeatureNotAvailableError):
        FeatureService.check_feature_access(user, "live_draft")


def test_metered_features_are_not_tier_gated():
    """Gate-semantics flip: trade/waiver/finder are NOT features in TIER_LIMITS
    anymore — every tier can use them (free pays credits via charge_metered)."""
    from backend.models.user import TIER_LIMITS
    for tier in TIER_LIMITS:
        for old_feature in ("trade_analyzer", "trade_finder", "waiver_wire"):
            assert old_feature not in TIER_LIMITS[tier]


def test_standard_can_access_live_draft():
    user = _FakeUser("standard")
    FeatureService.check_feature_access(user, "live_draft")  # no raise


def test_pro_can_access_cross_league_view():
    user = _FakeUser("pro")
    FeatureService.check_feature_access(user, "cross_league_view")  # no raise


def test_standard_cannot_access_cross_league_view():
    user = _FakeUser("standard")
    with pytest.raises(FeatureNotAvailableError):
        FeatureService.check_feature_access(user, "cross_league_view")


def test_expired_season_entitlement_gates_as_free():
    """A season purchase past tier_expires_at loses the entitlement."""
    from datetime import datetime, timedelta, timezone
    user = _FakeUser("standard")
    user.tier_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    with pytest.raises(FeatureNotAvailableError):
        FeatureService.check_feature_access(user, "live_draft")


def test_unexpired_season_entitlement_holds():
    from datetime import datetime, timedelta, timezone
    user = _FakeUser("standard")
    user.tier_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    FeatureService.check_feature_access(user, "live_draft")  # no raise


def test_standard_cannot_access_trade_finder():
    user = _FakeUser("standard")
    with pytest.raises(FeatureNotAvailableError) as exc_info:
        FeatureService.check_feature_access(user, "trade_finder")
    assert exc_info.value.detail["required_tier"] == "pro"


def test_all_tiers_can_access_injury_monitoring():
    for tier in ("free", "standard", "pro"):
        user = _FakeUser(tier)
        FeatureService.check_feature_access(user, "injury_monitoring")


def test_intro_cannot_access_live_draft():
    user = _FakeUser("free")
    with pytest.raises(FeatureNotAvailableError):
        FeatureService.check_feature_access(user, "live_draft")


def test_standard_can_access_live_draft():
    user = _FakeUser("standard")
    FeatureService.check_feature_access(user, "live_draft")


def test_can_add_league_within_limit():
    user = _FakeUser("standard")
    # Standard: max 1 league. Currently 0 — should pass.
    FeatureService.can_add_league(user, current_count=0)


def test_can_add_league_at_limit_raises():
    user = _FakeUser("standard")
    with pytest.raises(LeagueLimitError):
        FeatureService.can_add_league(user, current_count=2)


def test_pro_unlimited_leagues():
    user = _FakeUser("pro")
    # Pro: unlimited — should never raise
    FeatureService.can_add_league(user, current_count=100)


def test_intro_max_1_league():
    user = _FakeUser("free")
    with pytest.raises(LeagueLimitError):
        FeatureService.can_add_league(user, current_count=1)


def test_get_limits_returns_copy():
    user = _FakeUser("standard")
    limits = FeatureService.get_limits(user)
    assert limits["price_monthly_usd"] == 8
    # Ensure it's a copy, not the original
    limits["price_monthly_usd"] = 999
    assert FeatureService.get_limits(user)["price_monthly_usd"] == 8


def test_expired_season_pro_capped_as_free_for_leagues():
    """An expired season Pro must be capped at the FREE league limit, not Pro's
    unlimited — the league gate now reads effective_tier."""
    from datetime import datetime, timedelta, timezone
    user = _FakeUser("pro")
    user.tier_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    # Free cap is 1: adding a second league must raise (Pro would allow it).
    with pytest.raises(LeagueLimitError):
        FeatureService.can_add_league(user, current_count=1)


def test_unexpired_season_pro_keeps_unlimited_leagues():
    from datetime import datetime, timedelta, timezone
    user = _FakeUser("pro")
    user.tier_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    FeatureService.can_add_league(user, current_count=100)  # no raise


def test_get_limits_uses_effective_tier():
    """Expired season entitlement → get_limits reflects the free tier."""
    from datetime import datetime, timedelta, timezone
    from backend.models.user import TIER_LIMITS
    user = _FakeUser("pro")
    user.tier_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert FeatureService.get_limits(user)["max_leagues"] == TIER_LIMITS["free"]["max_leagues"]
