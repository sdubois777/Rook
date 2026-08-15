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

from backend.config import settings
from backend.core.dependencies import (
    get_credit_service,
    get_current_user,
    get_db,
    get_league_service,
)
from backend.models.user import (
    TIER_LIMITS,
    User,
    effective_tier,
    interval_is_referral_eligible,
)
from backend.repositories.user_repo import UserRepository

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
    subscription_status: Optional[str] = None  # billing state (read-only)
    tier_expires_at: str | None = None


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
    suspended: bool          # parked over the tier cap — readable, not usable
    last_synced: Optional[str]
    created_at: str
    draft_date: Optional[str] = None       # real synced draft date/time (ISO), None if unscheduled
    team_names: list[str] = []             # opponent/team names from the synced manager_map


class LeagueLimitStateResponse(BaseModel):
    over_limit: bool
    active_count: int
    max_leagues: Optional[int]     # None = unlimited (pro)
    candidates: list[LeagueResponse]  # current-season leagues (active + parked)


class ResolveLimitRequest(BaseModel):
    keep: list[str]  # league ids to keep active; the rest of the set is parked


class ReferralStateResponse(BaseModel):
    """The caller's own referral state. Every percentage is served from
    REFERRAL_PROGRAM via ReferralService — none is written down here, and the
    frontend renders these rather than restating them."""
    code: str
    share_url: str
    referral_count: int
    percent_off: int
    percent_off_cap: int
    percent_off_per_referral: int
    eligible: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserResponse)
async def get_me(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Current user profile and tier info (EFFECTIVE tier — a season purchase
    past its expiry reads as free, with a lazy write-back so the row converges
    without a cron)."""
    from backend.models.user import effective_tier

    eff = effective_tier(user)
    if eff != user.tier:
        # Season entitlement expired — persist the downgrade (credits persist).
        from backend.repositories.user_repo import UserRepository
        repo = UserRepository(db)
        await repo.update_tier(user.id, tier=eff, credits_bonus=0)
        await repo.set_tier_expiry(user.id, None)
        # A season purchase set subscription_status='active' and nothing clears it
        # on expiry (one-time payment → no subscription.deleted). Clear it in the
        # same write-back — don't leave a field asserting an active sub that ended.
        await repo.set_subscription_status(user.id, None)
        await repo.commit()
        user.tier = eff
        user.tier_expires_at = None
        user.subscription_status = None

    return UserResponse(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        tier=eff,
        credits_remaining=user.credits_remaining,
        tier_limits=TIER_LIMITS.get(eff, {}),
        subscription_status=getattr(user, "subscription_status", None),
        tier_expires_at=(
            user.tier_expires_at.isoformat()
            if getattr(user, "tier_expires_at", None) else None
        ),
    )


@router.get("/credits", response_model=CreditResponse)
async def get_credits(
    user: User = Depends(get_current_user),
    service=Depends(get_credit_service),
):
    """Credit balance and usage history."""
    history = await service.get_usage_history(user)
    used = sum(h.credits_used for h in history)
    # Monthly credit grants no longer exist (paid tiers are unlimited);
    # the field is kept at 0 for response-shape compatibility.
    monthly = 0

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


@router.get("/credentials")
async def get_connected_platforms(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Platforms this user has stored credentials for — drives the account page's
    per-platform Disconnect controls (Yahoo OAuth tokens / ESPN cookies). Returns
    only platform names, never token/cookie values."""
    from backend.repositories.credential_repo import CredentialRepository
    platforms = await CredentialRepository(db).list_platforms(user.id)
    return {"platforms": platforms}


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

    # Count ACTIVE (current-season, non-suspended) leagues only — matches the
    # connect paths. Finished history never counts against the cap.
    current_count = await service.count_active_leagues(user.id)
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
        token = await UserRepository(db).rotate_draft_token(user.id)
        return {"draft_token": token}
    return {"draft_token": user.draft_token}


@router.post("/draft-token/revoke")
async def revoke_draft_token(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Regenerate token — invalidates the old one."""
    token = await UserRepository(db).rotate_draft_token(user.id)
    return {"draft_token": token}


def _reward_applies_today(user: User) -> bool:
    """Whether the referrer's earned discount can be applied to this account now.

    The reward is a recurring coupon set on the referrer's own subscription, so
    it needs a monthly subscription to attach to. A free account has none, and a
    season pass is a one-time payment on an interval the program does not cover
    (interval_is_referral_eligible) — its buyer earns a rate that is recorded in
    code_redemptions but is not being applied to anything.

    Returned so the account page can say that plainly. The rate is still shown:
    it is earned, it survives, and it starts applying when they take a monthly
    plan. Without this flag the page would show a discount the user is not
    actually receiving, which is the one thing the referral design calls out as
    needing careful wording.
    """
    if effective_tier(user) == "free":
        return False
    if not getattr(user, "subscription_status", None):
        return False
    # tier_expires_at is set ONLY for a one-time season entitlement; a monthly
    # subscription leaves it NULL (see the User model).
    interval = "season" if getattr(user, "tier_expires_at", None) else "monthly"
    return interval_is_referral_eligible(interval)


@router.get("/referral", response_model=ReferralStateResponse)
async def get_referral(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """This user's own referral code, and what it has earned them so far.

    Scoped to the authenticated caller by user.id — there is no parameter that
    could name another account. It reports a COUNT and a RATE and nothing else:
    who redeemed the code is never returned, because the people who redeemed it
    consented to buy a subscription, not to have that purchase disclosed to the
    person who referred them.

    Codes are minted lazily on first read, so this endpoint is the write path
    for a brand-new code and commits. Repositories do not commit (house rule),
    so without this the INSERT would roll back at the end of the request and the
    next read would mint a different code — a user's shareable code would change
    under them, and links already sent out would stop resolving.
    """
    from backend.services.referral_service import ReferralService

    state = await ReferralService.from_session(db).referrer_state(user.id)
    await db.commit()

    return ReferralStateResponse(
        code=state["code"],
        # Lands on the marketing page, which reads ?ref= and keeps the code for
        # the signup round trip (Clerk's hosted signup makes no call we control).
        share_url=f"{settings.app_url}/?ref={state['code']}",
        referral_count=state["referral_count"],
        percent_off=state["percent_off"],
        percent_off_cap=state["percent_off_cap"],
        percent_off_per_referral=state["percent_off_per_referral"],
        eligible=_reward_applies_today(user),
    )


@router.delete("/leagues/{league_id}", status_code=204)
async def remove_league(
    league_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service=Depends(get_league_service),
):
    """Hard delete a league and all related data."""
    await service.delete_league(user.id, league_id)


# ---------------------------------------------------------------------------
# Tier-cap over-limit chooser (downgrade reconciliation)
# ---------------------------------------------------------------------------

def _reconciler(db):
    from backend.repositories.league_repo import LeagueRepository
    from backend.services.league_reconcile import LeagueReconciler
    repo = LeagueRepository(db)
    return LeagueReconciler(repo), repo


def _limit_state_response(state) -> LeagueLimitStateResponse:
    return LeagueLimitStateResponse(
        over_limit=state["over_limit"],
        active_count=state["active_count"],
        max_leagues=state["max_leagues"],
        candidates=[_league_response(lg) for lg in state["candidates"]],
    )


@router.get("/leagues/limit-state", response_model=LeagueLimitStateResponse)
async def get_league_limit_state(
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Over-limit snapshot for the forced chooser: how many active leagues vs the
    tier cap, and the current-season candidates (finished history excluded)."""
    reconciler, _ = _reconciler(db)
    # Effective tier — an expired season entitlement caps as free here too.
    state = await reconciler.limit_state(user.id, effective_tier(user))
    return _limit_state_response(state)


@router.post("/leagues/resolve-limit", response_model=LeagueLimitStateResponse)
async def resolve_league_limit(
    body: ResolveLimitRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Keep the chosen active leagues (<= cap); park the rest of the current-season
    set (suspended, never deleted). Idempotent; rejects keeping more than the cap."""
    from backend.core.exceptions import ValidationError

    reconciler, repo = _reconciler(db)
    try:
        keep_ids = [uuid.UUID(k) for k in body.keep]
    except ValueError:
        raise ValidationError("Invalid league id in keep list")

    eff = effective_tier(user)  # expired season caps as free for the chooser too
    await reconciler.resolve_keep(user.id, eff, keep_ids)
    await repo.commit()

    state = await reconciler.limit_state(user.id, eff)
    return _limit_state_response(state)


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
        suspended=league.suspended_at is not None,
        last_synced=(
            league.last_synced.isoformat()
            if league.last_synced else None
        ),
        created_at=league.created_at.isoformat(),
        draft_date=(
            league.draft_date.isoformat()
            if getattr(league, "draft_date", None) else None
        ),
        team_names=[n for n in (league.manager_map or {}).values() if n],
    )
