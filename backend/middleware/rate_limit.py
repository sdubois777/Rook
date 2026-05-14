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

from backend.core.exceptions import RateLimitError


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
        self._window = 60  # seconds
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


# Global limiters — different limits for different endpoint classes
_api_limiter = RateLimiter(requests_per_minute=120)      # General API
_pipeline_limiter = RateLimiter(requests_per_minute=5)   # Expensive ops
_auth_limiter = RateLimiter(requests_per_minute=10)      # Auth endpoints


def rate_limit_api(request: Request) -> None:
    """General API rate limit — 120 req/min per IP."""
    key = request.client.host if request.client else "unknown"
    _api_limiter.check(key)


def rate_limit_pipeline(request: Request) -> None:
    """Pipeline trigger rate limit — 5 req/min per IP."""
    key = request.client.host if request.client else "unknown"
    _pipeline_limiter.check(key)


def rate_limit_auth(request: Request) -> None:
    """Auth endpoint rate limit — 10 req/min per IP."""
    key = request.client.host if request.client else "unknown"
    _auth_limiter.check(key)
