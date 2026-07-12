"""Tests for tier-based league limits — Stage 28 specific."""
import pytest

from backend.core.exceptions import LeagueLimitError
from backend.services.feature_service import FeatureService


class _FakeUser:
    def __init__(self, tier: str):
        self.tier = tier


def test_free_user_limited_to_1_league():
    user = _FakeUser("free")
    # 0 leagues — should pass
    FeatureService.can_add_league(user, current_count=0)
    # 1 league — should raise
    with pytest.raises(LeagueLimitError):
        FeatureService.can_add_league(user, current_count=1)


def test_standard_user_limited_to_1_league():
    user = _FakeUser("standard")
    # 0 — should pass (standard cap is 1 under the new spec)
    FeatureService.can_add_league(user, current_count=0)
    # 1 — should raise
    with pytest.raises(LeagueLimitError):
        FeatureService.can_add_league(user, current_count=1)


def test_pro_user_unlimited_leagues():
    user = _FakeUser("pro")
    # Even absurd counts should pass
    FeatureService.can_add_league(user, current_count=0)
    FeatureService.can_add_league(user, current_count=10)
    FeatureService.can_add_league(user, current_count=100)


def test_free_at_limit_includes_max_in_error():
    user = _FakeUser("free")
    with pytest.raises(LeagueLimitError) as exc_info:
        FeatureService.can_add_league(user, current_count=1)
    assert "max_leagues" in exc_info.value.detail


def test_standard_at_limit_suggests_upgrade():
    user = _FakeUser("standard")
    with pytest.raises(LeagueLimitError) as exc_info:
        FeatureService.can_add_league(user, current_count=1)
    assert exc_info.value.detail["max_leagues"] == 1
