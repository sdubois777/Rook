"""
Domain exceptions for the Fantasy Football platform.

Raise these from service layer.
main.py registers handlers that map them to HTTP.
Never raise HTTPException from a service.
"""
from __future__ import annotations
from typing import Any


class AppError(Exception):
    """Base for all domain exceptions."""
    status_code: int = 500
    error_code: str = "internal_error"

    def __init__(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
    ):
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


class NotFoundError(AppError):
    status_code = 404
    error_code = "not_found"


class ConflictError(AppError):
    status_code = 409
    error_code = "conflict"


class UnauthorizedError(AppError):
    status_code = 401
    error_code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    error_code = "forbidden"


class InsufficientCreditsError(AppError):
    status_code = 402
    error_code = "insufficient_credits"

    def __init__(self, required: int, available: int):
        super().__init__(
            f"Insufficient credits: need {required}, have {available}",
            {
                "required": required,
                "available": available,
                "purchase_url": "/pricing",
            },
        )


class FeatureNotAvailableError(AppError):
    status_code = 403
    error_code = "feature_not_available"

    def __init__(self, feature: str, required_tier: str):
        super().__init__(
            f"Feature '{feature}' requires {required_tier} plan",
            {
                "feature": feature,
                "required_tier": required_tier,
                "upgrade_url": "/pricing",
            },
        )


class LeagueLimitError(AppError):
    status_code = 403
    error_code = "league_limit_reached"

    def __init__(self, current: int, max_leagues: int | None):
        super().__init__(
            f"League limit reached ({current} of {max_leagues})",
            {
                "current_leagues": current,
                "max_leagues": max_leagues,
                "upgrade_url": "/pricing",
            },
        )


class LeagueSuspendedError(AppError):
    status_code = 403
    error_code = "league_suspended"

    def __init__(self):
        super().__init__(
            "This league is parked over your plan's limit. Re-upgrade or choose "
            "it as an active league to use it again.",
            {"resolve_url": "/account"},
        )


class ValidationError(AppError):
    status_code = 422
    error_code = "validation_error"


class RateLimitError(AppError):
    status_code = 429
    error_code = "rate_limit_exceeded"

    def __init__(self, retry_after: int = 60):
        super().__init__(
            "Rate limit exceeded",
            {"retry_after_seconds": retry_after},
        )
