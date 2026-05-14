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
from typing import Annotated, Optional

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.exceptions import UnauthorizedError
from backend.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# HTTPBearer — extracts token from Authorization header
# auto_error=False so we can handle missing token ourselves
_bearer = HTTPBearer(auto_error=False)


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


# ── Clerk JWKS cache ────────────────────────────────────

_jwks_cache: dict | None = None


async def _get_clerk_jwks() -> dict:
    """
    Fetch Clerk's public JWKS keys for JWT verification.
    Keys rotate infrequently — cached in memory.
    Restart resets the cache (acceptable).
    """
    import base64

    from backend.config import settings

    pub_key = settings.vite_clerk_publishable_key or ""

    if pub_key.startswith("pk_test_") or pub_key.startswith("pk_live_"):
        try:
            key_part = pub_key.split("_", 2)[2]
            padded = key_part + "=" * (-len(key_part) % 4)
            instance_url = base64.b64decode(padded).decode().rstrip("$")
            jwks_url = f"https://{instance_url}/.well-known/jwks.json"
        except Exception:
            jwks_url = "https://api.clerk.dev/v1/jwks"
    else:
        jwks_url = "https://api.clerk.dev/v1/jwks"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        return resp.json()


async def _verify_clerk_jwt(token: str) -> dict:
    """
    Verify a Clerk JWT token and return decoded payload dict.
    Raises UnauthorizedError on invalid token.
    """
    global _jwks_cache

    try:
        if _jwks_cache is None:
            _jwks_cache = await _get_clerk_jwks()

        payload = jwt.decode(
            token,
            _jwks_cache,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )

        user_id = payload.get("sub")
        if not user_id:
            raise UnauthorizedError("Token missing sub claim")

        return payload

    except JWTError as e:
        _jwks_cache = None
        logger.warning("JWT verification failed: %s", e)
        raise UnauthorizedError("Invalid or expired token")
    except UnauthorizedError:
        raise
    except Exception as e:
        _jwks_cache = None
        logger.error("JWT verification error: %s", e)
        raise UnauthorizedError("Authentication failed")


# ── Clerk user email lookup ────────────────────────────────

# Cache email lookups — one Clerk API call per user per server restart
_email_cache: dict[str, str | None] = {}


async def _fetch_clerk_user_email(user_id: str) -> str | None:
    """Fetch user email from Clerk Backend API using the secret key."""
    if user_id in _email_cache:
        return _email_cache[user_id]

    from backend.config import settings

    secret = settings.clerk_secret_key
    if not secret:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.clerk.com/v1/users/{user_id}",
                headers={"Authorization": f"Bearer {secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
            primary_id = data.get("primary_email_address_id")
            for addr in data.get("email_addresses", []):
                if addr.get("id") == primary_id:
                    email = addr["email_address"]
                    _email_cache[user_id] = email
                    return email
    except Exception as e:
        logger.warning("Failed to fetch Clerk user email: %s", e)

    _email_cache[user_id] = None
    return None


# ── Auth ─────────────────────────────────────────────────

async def get_current_user_id(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """
    Extract and verify user identity from request.

    Production: verifies Clerk JWT from Authorization header.
    Development (no Clerk configured): uses X-User-Id header.

    Returns dict with 'user_id' and optional 'email'.
    """
    from backend.config import settings

    # Development fallback — only when Clerk not configured
    if not settings.clerk_enabled:
        if settings.environment == "production":
            raise UnauthorizedError("CLERK_SECRET_KEY not configured")
        user_id = request.headers.get("X-User-Id", "dev-user-001")
        logger.debug("Dev auth: user_id=%s", user_id)
        return {"user_id": user_id, "email": f"{user_id}@dev.local"}

    # Production path — verify Clerk JWT
    if not credentials:
        raise UnauthorizedError("Authorization header required")

    payload = await _verify_clerk_jwt(credentials.credentials)
    user_id = payload["sub"]

    # Clerk default JWT has no email — fetch from Clerk Backend API
    email = payload.get("email") or payload.get("email_address")
    if not email:
        email = await _fetch_clerk_user_email(user_id)

    return {"user_id": user_id, "email": email}


async def get_current_user(
    auth: dict = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the User DB record for the current request.
    Creates the user record if this is their first request.
    """
    from backend.repositories.user_repo import UserRepository
    from backend.services.user_service import UserService

    user_id = auth["user_id"]
    email = auth.get("email") or f"{user_id}@placeholder.local"

    repo = UserRepository(db)
    service = UserService(repo)
    user, _ = await service.get_or_create(
        external_id=user_id,
        email=email,
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
