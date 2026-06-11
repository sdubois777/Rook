"""
Simple in-memory rate limiter.
Per-user limits on expensive endpoints.

For production scale, replace with Redis-backed limiter.
The interface is identical — swap the store only.
"""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock

from fastapi import Request

from backend.config import settings
from backend.core.exceptions import RateLimitError

WINDOW_SECONDS = 60


class RateLimiter:
    """
    Token bucket rate limiter.
    Thread-safe for single-process deployments.
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
    ):
        self._rpm = requests_per_minute
        self._window = WINDOW_SECONDS
        self._buckets: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def check(self, key: str) -> None:
        """
        Check if key is within rate limit.
        Raises RateLimitError if exceeded.
        """
        now = time.time()
        window_start = now - self._window

        with self._lock:
            # Remove timestamps outside window
            self._buckets[key] = [
                t for t in self._buckets[key]
                if t > window_start
            ]

            if len(self._buckets[key]) >= self._rpm:
                oldest = self._buckets[key][0]
                retry_after = int(self._window - (now - oldest)) + 1
                raise RateLimitError(retry_after=retry_after)

            self._buckets[key].append(now)


# Global limiters — limits per endpoint class, tunable via config
_api_limiter = RateLimiter(requests_per_minute=settings.rate_limit_api_rpm)
_pipeline_limiter = RateLimiter(requests_per_minute=settings.rate_limit_pipeline_rpm)
_auth_limiter = RateLimiter(requests_per_minute=settings.rate_limit_auth_rpm)


def rate_limit_api(request: Request) -> None:
    """General API rate limit per IP."""
    key = request.client.host if request.client else "unknown"
    _api_limiter.check(key)


def rate_limit_pipeline(request: Request) -> None:
    """Pipeline trigger rate limit per IP."""
    key = request.client.host if request.client else "unknown"
    _pipeline_limiter.check(key)


def rate_limit_auth(request: Request) -> None:
    """Auth endpoint rate limit per IP."""
    key = request.client.host if request.client else "unknown"
    _auth_limiter.check(key)
