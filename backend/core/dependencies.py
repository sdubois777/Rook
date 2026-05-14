"""
FastAPI dependency injection functions.

All routes receive their dependencies from here.
Import and use with Depends():

    from backend.core.dependencies import get_db, get_current_user

    @router.get("/me")
    async def me(user: User = Depends(get_current_user)):
        ...

Dependency graph:
  get_db → yields AsyncSession
  get_current_user_id(request) → str (Clerk ID)
  get_current_user(user_id, db) → User (DB record)
  get_user_repo(db) → UserRepository
  get_credit_service(user_repo, credit_repo) → CreditService
  require_feature(feature)(user) → None or raises
  require_credits(action)(user, service) → None or raises
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import UnauthorizedError
from backend.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


# ── Database ────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields an async DB session for the request lifetime.
    Session is automatically closed after the request.
    Use this as the base dependency for all DB access.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


DB = Annotated[AsyncSession, Depends(get_db)]


# ── Auth ─────────────────────────────────────────────────
# Stub — replaced by real Clerk verification in Stage 26.
# All protected routes use get_current_user today and will
# automatically get real auth when Stage 26 wires it in.

async def get_current_user_id(request: Request) -> str:
    """
    Extract user identity from request.

    Stage 25: reads X-User-Id header (dev only).
    Stage 26: replaces with Clerk JWT verification.

    NEVER expose X-User-Id header in production.
    Railway environment check enforced.
    """
    from backend.config import settings

    if settings.environment == "production":
        # In production, this stub is intentionally broken.
        # Stage 26 will replace it with real Clerk auth.
        # If this raises in production, auth isn't wired up.
        raise UnauthorizedError(
            "Authentication not configured — Stage 26 required"
        )

    # Development only — allow header override for testing
    user_id = request.headers.get("X-User-Id", "dev-user-001")
    return user_id


async def get_current_user(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the User DB record for the current request.
    Creates the user record if this is their first request.

    Stage 26 replaces the user_id source but this
    function signature stays identical.
    """
    from backend.repositories.user_repo import UserRepository
    from backend.services.user_service import UserService

    repo = UserRepository(db)
    service = UserService(repo)
    user, _ = await service.get_or_create(
        external_id=user_id,
        email=f"{user_id}@dev.local",
    )
    return user


# ── Repository factories ─────────────────────────────────

async def get_user_repo(
    db: AsyncSession = Depends(get_db),
):
    from backend.repositories.user_repo import UserRepository
    return UserRepository(db)


async def get_credit_repo(
    db: AsyncSession = Depends(get_db),
):
    from backend.repositories.credit_repo import CreditRepository
    return CreditRepository(db)


async def get_league_repo(
    db: AsyncSession = Depends(get_db),
):
    from backend.repositories.league_repo import LeagueRepository
    return LeagueRepository(db)


# ── Service factories ────────────────────────────────────

async def get_credit_service(
    user_repo=Depends(get_user_repo),
    credit_repo=Depends(get_credit_repo),
):
    from backend.services.credit_service import CreditService
    return CreditService(user_repo, credit_repo)


async def get_league_service(
    league_repo=Depends(get_league_repo),
):
    from backend.services.league_service import LeagueService
    return LeagueService(league_repo)


# ── Guard dependencies ───────────────────────────────────

def require_feature(feature: str):
    """
    Dependency factory — raises FeatureNotAvailableError
    if the current user's tier does not include the feature.

    Usage:
        @router.post("/trade/analyze")
        async def analyze(
            _: None = Depends(require_feature("trade_analyzer")),
            user: User = Depends(get_current_user),
        ):
    """
    async def _check(user=Depends(get_current_user)):
        from backend.services.feature_service import FeatureService
        FeatureService.check_feature_access(user, feature)
    return _check


def require_credits(action: str):
    """
    Dependency factory — deducts credits for an action.
    Raises InsufficientCreditsError if balance too low.
    Always checks feature access before credits.

    Usage:
        @router.post("/trade/analyze")
        async def analyze(
            _: None = Depends(require_credits("trade_analysis")),
        ):
    """
    async def _check(
        user=Depends(get_current_user),
        service=Depends(get_credit_service),
    ):
        from backend.services.feature_service import FeatureService
        from backend.models.user import CREDIT_COSTS

        # Infer feature from action
        feature_map = {
            "trade_analysis": "trade_analyzer",
            "trade_finder": "trade_finder",
            "waiver_wire": "waiver_wire",
        }
        feature = feature_map.get(action)
        if feature:
            FeatureService.check_feature_access(user, feature)

        cost = CREDIT_COSTS.get(action, 0)
        if cost > 0:
            await service.deduct(user, action)
    return _check
