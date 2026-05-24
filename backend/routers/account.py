"""
Account router — user profile, credits, leagues.

All endpoints require authentication.
All data is scoped to the current user.
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import uuid as uuid_mod

from backend.core.dependencies import (
    get_credit_service,
    get_current_user,
    get_db,
    get_league_service,
)
from backend.models.user import TIER_LIMITS, User

router = APIRouter(prefix="/account", tags=["account"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class UserResponse(BaseModel):
    id: str
    email: str
    display_name: Optional[str] = None
    tier: str
    credits_remaining: int
    tier_limits: dict


class CreditUsageItem(BaseModel):
    action: str
    credits_used: int
    created_at: str


class CreditResponse(BaseModel):
    balance: int
    monthly_allowance: int
    usage_last_30_days: int
    history: list[CreditUsageItem]


class LeagueCreate(BaseModel):
    platform: Literal["yahoo", "espn", "sleeper"]
    league_id: str = Field(..., min_length=1, max_length=100)
    league_name: Optional[str] = None
    team_count: int = Field(default=12, ge=6, le=20)
    draft_type: Literal["auction", "snake"] = "auction"
    scoring: Literal["ppr", "half_ppr", "standard"] = "ppr"
    budget: Optional[int] = Field(default=200, ge=50, le=500)
    season_year: int = Field(ge=2020, le=2035)


class LeagueResponse(BaseModel):
    id: str
    platform: str
    league_id: str
    league_name: Optional[str]
    team_count: int
    draft_type: str
    scoring: str
    budget: Optional[int]
    season_year: int
    is_active: bool
    last_synced: Optional[str]
    created_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserResponse)
async def get_me(
    user: User = Depends(get_current_user),
):
    """Current user profile and tier info."""
    return UserResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        tier=user.tier,
        credits_remaining=user.credits_remaining,
        tier_limits=TIER_LIMITS.get(user.tier, {}),
    )


@router.get("/credits", response_model=CreditResponse)
async def get_credits(
    user: User = Depends(get_current_user),
    service=Depends(get_credit_service),
):
    """Credit balance and usage history."""
    history = await service.get_usage_history(user)
    used = sum(h.credits_used for h in history)
    monthly = TIER_LIMITS.get(
        user.tier, {}
    ).get("credits_monthly", 0)

    return CreditResponse(
        balance=user.credits_remaining,
        monthly_allowance=monthly,
        usage_last_30_days=used,
        history=[
            CreditUsageItem(
                action=h.action,
                credits_used=h.credits_used,
                created_at=h.created_at.isoformat(),
            )
            for h in history
        ],
    )


@router.get("/leagues", response_model=list[LeagueResponse])
async def get_leagues(
    user: User = Depends(get_current_user),
    service=Depends(get_league_service),
):
    """All active leagues for current user."""
    leagues = await service.get_user_leagues(user.id)
    return [_league_response(league) for league in leagues]


@router.post(
    "/leagues",
    response_model=LeagueResponse,
    status_code=201,
)
async def add_league(
    body: LeagueCreate,
    user: User = Depends(get_current_user),
    service=Depends(get_league_service),
):
    """
    Add a new league.
    Checks tier limit before creating.
    Standard: max 2 leagues. Pro: unlimited.
    """
    from backend.services.feature_service import FeatureService

    current_count = len(
        await service.get_user_leagues(user.id)
    )
    FeatureService.can_add_league(user, current_count)

    league = await service.add_league(
        user_id=user.id,
        platform=body.platform,
        league_id=body.league_id,
        team_count=body.team_count,
        draft_type=body.draft_type,
        scoring=body.scoring,
        budget=body.budget,
        season_year=body.season_year,
    )
    return _league_response(league)


@router.get("/draft-token")
async def get_draft_token(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """
    Returns user's draft token. Creates one if it doesn't exist.
    Long-lived UUID used by the browser extension to authenticate
    without a session.
    """
    if not user.draft_token:
        from backend.models.user import User as UserModel
        db_user = await db.get(UserModel, user.id)
        db_user.draft_token = str(uuid_mod.uuid4())
        await db.commit()
        return {"draft_token": db_user.draft_token}
    return {"draft_token": user.draft_token}


@router.post("/draft-token/revoke")
async def revoke_draft_token(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Regenerate token — invalidates the old one."""
    from backend.models.user import User as UserModel
    db_user = await db.get(UserModel, user.id)
    db_user.draft_token = str(uuid_mod.uuid4())
    await db.commit()
    return {"draft_token": db_user.draft_token}


@router.delete("/leagues/{league_id}", status_code=204)
async def remove_league(
    league_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service=Depends(get_league_service),
):
    """Hard delete a league and all related data."""
    await service.delete_league(user.id, league_id)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _league_response(league) -> LeagueResponse:
    """Convert UserLeague ORM object to response schema."""
    return LeagueResponse(
        id=str(league.id),
        platform=league.platform,
        league_id=league.league_id,
        league_name=league.league_name,
        team_count=league.team_count,
        draft_type=league.draft_type,
        scoring=league.scoring,
        budget=league.budget,
        season_year=league.season_year,
        is_active=league.is_active,
        last_synced=(
            league.last_synced.isoformat()
            if league.last_synced else None
        ),
        created_at=league.created_at.isoformat(),
    )
