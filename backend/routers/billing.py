"""
Billing router — Stripe Checkout + Customer Portal session creation.

Auth-required and rate-limited (§0.E). Card data never touches this server: both
endpoints just create a Stripe-hosted session and return its URL for redirect
(§0.A). The customer is bound to the authenticated user's row server-side (§0.C);
the client supplies only a tier/pack NAME — never a price id or amount (§0.B). The
success-return URL grants nothing — entitlement flips solely in the webhook.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator

from backend.config import settings
from backend.core.dependencies import get_current_user, get_db
from backend.core.exceptions import ValidationError
from backend.middleware.rate_limit import rate_limit_auth
from backend.models.user import User
from backend.repositories.user_repo import UserRepository
from backend.services.billing import catalog, stripe_gateway

# proration_date reuse window: the confirm must reuse the preview's timestamp so
# the charge matches, but a client-supplied far-future value (e.g. period_end)
# would zero out the proration — a free upgrade. Accept only a recent, non-future
# timestamp within the current period.
_PRORATION_MAX_AGE_S = 3600
_PRORATION_FUTURE_SKEW_S = 60

router = APIRouter(
    prefix="/billing",
    tags=["billing"],
    dependencies=[Depends(rate_limit_auth)],  # §0.E — blunt abuse of session creation
)


class CheckoutRequest(BaseModel):
    """Exactly one of tier / pack. Both are server-mapped to a price id — the
    client can NEVER supply a price id or amount."""
    tier: Optional[Literal["intro", "standard", "pro"]] = None
    pack: Optional[Literal["small", "medium", "large"]] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.tier) == bool(self.pack):
            raise ValueError("Provide exactly one of 'tier' or 'pack'")
        return self


class CheckoutResponse(BaseModel):
    url: str


class PortalResponse(BaseModel):
    url: str


def _require_stripe() -> None:
    if not settings.stripe_enabled:
        raise HTTPException(status_code=503, detail="Billing is not configured")


async def _ensure_customer(user: User, db) -> str:
    """Return the user's Stripe customer id, creating + persisting one if absent.

    The customer is bound to the authenticated user (email + our ids in metadata);
    it is never accepted from the request.
    """
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer_id = stripe_gateway.create_customer(
        email=user.email,
        external_id=user.external_id,
        user_id=str(user.id),
        idempotency_key=f"cust_{user.id}",
    )
    repo = UserRepository(db)
    await repo.set_stripe_customer_id(user.id, customer_id)
    await repo.commit()
    user.stripe_customer_id = customer_id
    return customer_id


def _create_pack_session(user: User, customer_id: str, pack: str) -> str:
    """Create a one-time (mode=payment) credit-pack Checkout session; return URL.

    Shared by /checkout (pack branch) and /checkout-pack. The pack NAME maps to a
    server-configured price; the credit amount rides in metadata for the webhook.
    """
    price_id = catalog.pack_to_price(pack)
    credits = catalog.pack_to_credits(pack)
    if not price_id or credits is None:
        raise HTTPException(
            status_code=400, detail=f"No price configured for pack '{pack}'"
        )
    return stripe_gateway.create_checkout_session(
        customer_id=customer_id,
        mode="payment",
        price_id=price_id,
        success_url=f"{settings.app_url}/account?billing=success",
        cancel_url=f"{settings.app_url}/account?billing=cancel",
        metadata={"pack": pack, "credits": str(credits), "user_id": str(user.id)},
        # Unique per attempt: a Checkout session doesn't charge (the hosted page
        # does), so each purchase must get a FRESH session. A stable key would
        # return the prior, already-completed session ("you're all done here").
        idempotency_key=f"co_{user.id}_pack_{pack}_{uuid.uuid4()}",
    )


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    body: CheckoutRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a Checkout Session for a subscription tier or a credit pack."""
    _require_stripe()
    customer_id = await _ensure_customer(user, db)

    if body.pack:
        return CheckoutResponse(url=_create_pack_session(user, customer_id, body.pack))

    price_id = catalog.tier_to_price(body.tier)
    if not price_id:
        raise HTTPException(
            status_code=400,
            detail=f"No price configured for tier '{body.tier}'",
        )
    url = stripe_gateway.create_checkout_session(
        customer_id=customer_id,
        mode="subscription",
        price_id=price_id,
        # These pages grant NOTHING (§0.B) — the webhook is the only grantor.
        success_url=f"{settings.app_url}/account?billing=success",
        cancel_url=f"{settings.app_url}/pricing?billing=cancel",
        metadata={"tier": body.tier, "user_id": str(user.id)},
        # Fresh session per attempt (see _create_pack_session).
        idempotency_key=f"co_{user.id}_tier_{body.tier}_{uuid.uuid4()}",
    )
    return CheckoutResponse(url=url)


class CheckoutPackRequest(BaseModel):
    pack: Literal["small", "medium", "large"]


@router.post("/checkout-pack", response_model=CheckoutResponse)
async def checkout_pack(
    body: CheckoutPackRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """One-time credit-pack purchase via Checkout (mode=payment). The card is
    collected on Stripe's page; the webhook grants the credits once on success."""
    _require_stripe()
    customer_id = await _ensure_customer(user, db)
    return CheckoutResponse(url=_create_pack_session(user, customer_id, body.pack))


# ── Change plan (preview + confirm) ─────────────────────────────────────

class ChangePlanRequest(BaseModel):
    target_tier: Literal["intro", "standard", "pro"]


class ChangePlanConfirmRequest(BaseModel):
    target_tier: Literal["intro", "standard", "pro"]
    proration_date: Optional[int] = None  # from preview; required for upgrades


class ChangePlanPreviewResponse(BaseModel):
    direction: Literal["upgrade", "downgrade"]
    amount_due_today: int          # cents; 0 for a downgrade
    currency: str
    effective: str                 # "now" (upgrade) | ISO period-end (downgrade)
    proration_date: Optional[int]  # echo for confirm (upgrade only)
    target_tier: str
    active_leagues: int            # user's current active-league count
    max_active_leagues: Optional[int]  # target tier's cap (None = unlimited)


class ChangePlanConfirmResponse(BaseModel):
    status: Literal["applied", "scheduled"]
    effective: str
    target_tier: str


def _change_plan_context(user: User, target_tier: str):
    """Shared guards for preview/confirm: active sub, real direction, target price,
    subscription snapshot. Returns (is_upgrade, target_price_id, snapshot)."""
    if not user.stripe_subscription_id:
        raise ValidationError("No active subscription to change — subscribe first")
    direction = catalog.is_upgrade(user.tier, target_tier)
    if direction is None:
        raise HTTPException(
            status_code=400, detail="Target tier must differ from the current tier"
        )
    target_price = catalog.tier_to_price(target_tier)
    if not target_price:
        raise HTTPException(
            status_code=400, detail=f"No price configured for tier '{target_tier}'"
        )
    snap = stripe_gateway.subscription_snapshot(user.stripe_subscription_id)
    return direction, target_price, snap


@router.post("/change-plan/preview", response_model=ChangePlanPreviewResponse)
async def change_plan_preview(
    body: ChangePlanRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Preview a tier change. Charges nothing, changes nothing. Upgrades return the
    exact prorated amount due today plus the proration_date to reuse on confirm;
    downgrades return the period-end effective date. Also reports the user's active-
    league count vs the target cap so the UI can warn about a forced chooser."""
    _require_stripe()
    is_up, target_price, snap = _change_plan_context(user, body.target_tier)

    from backend.models.user import TIER_LIMITS
    from backend.repositories.league_repo import LeagueRepository
    active_leagues = await LeagueRepository(db).count_active(user.id)
    max_active = TIER_LIMITS.get(body.target_tier, {}).get("max_leagues")

    if is_up:
        proration_date = int(time.time())
        amount = stripe_gateway.preview_upgrade_amount(
            customer_id=user.stripe_customer_id,
            sub_id=user.stripe_subscription_id,
            item_id=snap["item_id"],
            target_price_id=target_price,
            proration_date=proration_date,
        )
        return ChangePlanPreviewResponse(
            direction="upgrade", amount_due_today=amount, currency="usd",
            effective="now", proration_date=proration_date,
            target_tier=body.target_tier,
            active_leagues=active_leagues, max_active_leagues=max_active,
        )

    effective = datetime.fromtimestamp(
        snap["period_end"], tz=timezone.utc
    ).isoformat()
    return ChangePlanPreviewResponse(
        direction="downgrade", amount_due_today=0, currency="usd",
        effective=effective, proration_date=None, target_tier=body.target_tier,
        active_leagues=active_leagues, max_active_leagues=max_active,
    )


@router.post("/change-plan/confirm", response_model=ChangePlanConfirmResponse)
async def change_plan_confirm(
    body: ChangePlanConfirmRequest,
    user: User = Depends(get_current_user),
):
    """Apply a previewed change. Upgrade: swap price now, prorated + invoiced
    immediately against the card on file, reusing the preview's proration_date.
    Downgrade: schedule the drop at period-end (no charge/refund). Never writes
    users.tier — the verified webhook is the sole tier-writer (§0.B)."""
    _require_stripe()
    is_up, target_price, snap = _change_plan_context(user, body.target_tier)

    if is_up:
        pd = body.proration_date
        now = int(time.time())
        valid = (
            pd is not None
            and snap["period_start"] <= pd <= snap["period_end"]
            and now - _PRORATION_MAX_AGE_S <= pd <= now + _PRORATION_FUTURE_SKEW_S
        )
        if not valid:
            raise HTTPException(
                status_code=400,
                detail="Invalid or stale proration_date — re-preview and retry",
            )
        stripe_gateway.apply_upgrade(
            sub_id=user.stripe_subscription_id,
            item_id=snap["item_id"],
            target_price_id=target_price,
            proration_date=pd,
            idempotency_key=f"chg_{user.id}_{body.target_tier}_{pd}",
        )
        return ChangePlanConfirmResponse(
            status="applied", effective="now", target_tier=body.target_tier
        )

    result = stripe_gateway.schedule_downgrade(
        sub_id=user.stripe_subscription_id,
        current_price_id=snap["price_id"],
        target_price_id=target_price,
        idempotency_key=f"chg_{user.id}_{body.target_tier}_{snap['period_end']}",
    )
    effective = datetime.fromtimestamp(
        result["effective"], tz=timezone.utc
    ).isoformat()
    return ChangePlanConfirmResponse(
        status="scheduled", effective=effective, target_tier=body.target_tier
    )


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    user: User = Depends(get_current_user),
):
    """Create a Customer Portal session (manage/cancel/update card)."""
    _require_stripe()
    if not user.stripe_customer_id:
        raise ValidationError("No billing account for this user")

    url = stripe_gateway.create_portal_session(
        customer_id=user.stripe_customer_id,
        return_url=f"{settings.app_url}/account",
    )
    return PortalResponse(url=url)
