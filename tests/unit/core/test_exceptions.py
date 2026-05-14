"""Tests for domain exception hierarchy."""
from backend.core.exceptions import (
    AppError,
    InsufficientCreditsError,
    FeatureNotAvailableError,
    NotFoundError,
    RateLimitError,
    LeagueLimitError,
    ValidationError,
)


def test_app_error_has_status_code():
    err = AppError("boom")
    assert err.status_code == 500
    assert err.error_code == "internal_error"
    assert err.message == "boom"
    assert err.detail == {}


def test_not_found_has_404():
    err = NotFoundError("Player not found")
    assert err.status_code == 404
    assert err.error_code == "not_found"


def test_insufficient_credits_includes_amounts():
    err = InsufficientCreditsError(required=10, available=3)
    assert err.status_code == 402
    assert err.error_code == "insufficient_credits"
    assert err.detail["required"] == 10
    assert err.detail["available"] == 3
    assert "10" in err.message
    assert "3" in err.message
    assert err.detail["purchase_url"] == "/pricing"


def test_feature_not_available_includes_tier():
    err = FeatureNotAvailableError(feature="trade_analyzer", required_tier="standard")
    assert err.status_code == 403
    assert err.error_code == "feature_not_available"
    assert err.detail["feature"] == "trade_analyzer"
    assert err.detail["required_tier"] == "standard"
    assert err.detail["upgrade_url"] == "/pricing"


def test_league_limit_error():
    err = LeagueLimitError(current=2, max_leagues=2)
    assert err.status_code == 403
    assert err.detail["current_leagues"] == 2
    assert err.detail["max_leagues"] == 2


def test_rate_limit_error_default_retry():
    err = RateLimitError()
    assert err.status_code == 429
    assert err.detail["retry_after_seconds"] == 60


def test_rate_limit_error_custom_retry():
    err = RateLimitError(retry_after=30)
    assert err.detail["retry_after_seconds"] == 30


def test_validation_error():
    err = ValidationError("bad input")
    assert err.status_code == 422
    assert err.error_code == "validation_error"


def test_app_error_with_detail():
    err = AppError("oops", detail={"field": "name"})
    assert err.detail["field"] == "name"
