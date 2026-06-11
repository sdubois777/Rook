"""Tests for backend/middleware/rate_limit.py"""
from __future__ import annotations

import pytest

from backend.core.exceptions import RateLimitError
from backend.middleware.rate_limit import WINDOW_SECONDS, RateLimiter


def test_rate_limiter_allows_requests_under_limit():
    """Requests below the per-minute limit all pass."""
    limiter = RateLimiter(requests_per_minute=5)
    for _ in range(5):
        limiter.check("1.2.3.4")


def test_rate_limiter_raises_when_limit_exceeded():
    """The request past the limit raises RateLimitError with retry info."""
    limiter = RateLimiter(requests_per_minute=3)
    for _ in range(3):
        limiter.check("1.2.3.4")

    with pytest.raises(RateLimitError) as exc_info:
        limiter.check("1.2.3.4")

    retry_after = exc_info.value.detail["retry_after_seconds"]
    assert 0 < retry_after <= WINDOW_SECONDS + 1


def test_rate_limiter_keys_are_independent():
    """One client's exhausted bucket never blocks another client."""
    limiter = RateLimiter(requests_per_minute=1)
    limiter.check("1.1.1.1")
    limiter.check("2.2.2.2")

    with pytest.raises(RateLimitError):
        limiter.check("1.1.1.1")


def test_rate_limiter_window_expiry_allows_requests_again(monkeypatch):
    """Timestamps older than the window are evicted, freeing capacity."""
    limiter = RateLimiter(requests_per_minute=1)

    fake_now = {"t": 1000.0}
    monkeypatch.setattr(
        "backend.middleware.rate_limit.time.time", lambda: fake_now["t"]
    )

    limiter.check("1.2.3.4")
    with pytest.raises(RateLimitError):
        limiter.check("1.2.3.4")

    fake_now["t"] += WINDOW_SECONDS + 1
    limiter.check("1.2.3.4")
