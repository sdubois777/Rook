"""Tests for FeatureService — tier-based access control."""
import pytest

from backend.core.exceptions import FeatureNotAvailableError, LeagueLimitError
from backend.services.feature_service import FeatureService


class _FakeUser:
    """Minimal user object for testing."""
    def __init__(self, tier: str):
        self.tier = tier


def test_intro_cannot_access_trade_analyzer():
    user = _FakeUser("intro")
    with pytest.raises(FeatureNotAvailableError) as exc_info:
        FeatureService.check_feature_access(user, "trade_analyzer")
    assert exc_info.value.detail["required_tier"] == "standard"


def test_standard_can_access_trade_analyzer():
    user = _FakeUser("standard")
    # Should not raise
    FeatureService.check_feature_access(user, "trade_analyzer")


def test_pro_can_access_trade_finder():
    user = _FakeUser("pro")
    FeatureService.check_feature_access(user, "trade_finder")


def test_standard_cannot_access_trade_finder():
    user = _FakeUser("standard")
    with pytest.raises(FeatureNotAvailableError) as exc_info:
        FeatureService.check_feature_access(user, "trade_finder")
    assert exc_info.value.detail["required_tier"] == "pro"


def test_all_tiers_can_access_injury_monitoring():
    for tier in ("intro", "standard", "pro"):
        user = _FakeUser(tier)
        FeatureService.check_feature_access(user, "injury_monitoring")


def test_intro_cannot_access_live_draft():
    user = _FakeUser("intro")
    with pytest.raises(FeatureNotAvailableError):
        FeatureService.check_feature_access(user, "live_draft")


def test_standard_can_access_live_draft():
    user = _FakeUser("standard")
    FeatureService.check_feature_access(user, "live_draft")


def test_can_add_league_within_limit():
    user = _FakeUser("standard")
    # Standard: max 2 leagues. Currently 1 — should pass.
    FeatureService.can_add_league(user, current_count=1)


def test_can_add_league_at_limit_raises():
    user = _FakeUser("standard")
    with pytest.raises(LeagueLimitError):
        FeatureService.can_add_league(user, current_count=2)


def test_pro_unlimited_leagues():
    user = _FakeUser("pro")
    # Pro: unlimited — should never raise
    FeatureService.can_add_league(user, current_count=100)


def test_intro_max_1_league():
    user = _FakeUser("intro")
    with pytest.raises(LeagueLimitError):
        FeatureService.can_add_league(user, current_count=1)


def test_get_limits_returns_copy():
    user = _FakeUser("standard")
    limits = FeatureService.get_limits(user)
    assert limits["credits_monthly"] == 20
    # Ensure it's a copy, not the original
    limits["credits_monthly"] = 999
    assert FeatureService.get_limits(user)["credits_monthly"] == 20
