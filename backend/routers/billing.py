"""
Billing router — Stripe Checkout + Customer Portal session creation.

Auth-required and rate-limited (§0.E). Card data never touches this server: both
endpoints just create a Stripe-hosted session and return its URL for redirect
(§0.A). The customer is bound to the authenticated user's row server-side (§0.C);
the client supplies only a tier/pack NAME — never a price id or amount (§0.B). The
success-return URL grants nothing — entitlement flips solely in the webhook.
"""
from __future__ import annotations

import logging
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
from backend.models.user import CREDIT_PACKS, User
from backend.repositories.user_repo import UserRepository
from backend.services.billing import catalog, stripe_gateway

logger = logging.getLogger(__name__)

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

# Public, unauthenticated, read-only: the pricing sheet the frontend renders
# (landing page + account). Serves ONLY backend/models/user.py — the single
# source of truth — so no dollar amount, credit cost, grant, or pack size is
# ever hardcoded client-side again.
public_router = APIRouter(prefix="/billing", tags=["billing"])


@public_router.get("/pricing")
async def get_pricing():
    from backend.models.user import (
        CREDIT_COSTS, CREDIT_PACKS, REFERRAL_PROGRAM, TIER_LIMITS, TIER_ORDER,
    )

    return {
        "tiers": [
            {
                "id": tier,
                "label": TIER_LIMITS[tier]["label"],
                "price_monthly_usd": TIER_LIMITS[tier]["price_monthly_usd"],
                "price_season_usd": TIER_LIMITS[tier]["price_season_usd"],
                "max_leagues": TIER_LIMITS[tier]["max_leagues"],
                "unlimited_features": TIER_LIMITS[tier]["unlimited_features"],
                "live_draft": TIER_LIMITS[tier]["live_draft"],
                "cross_league_view": TIER_LIMITS[tier]["cross_league_view"],
                "credits_signup_bonus": TIER_LIMITS[tier]["credits_signup_bonus"],
            }
            for tier in TIER_ORDER
        ],
        "credit_costs": dict(CREDIT_COSTS),
        "packs": [
            {"id": name, "price_usd": p["price_usd"], "credits": p["credits"]}
            for name, p in CREDIT_PACKS.items()
        ],
        # Referral percentages, served from REFERRAL_PROGRAM so the frontend
        # renders them instead of restating them. A number typed into a React
        # component is the drift this block exists to prevent.
        "referral": {
            "welcome_percent_off": REFERRAL_PROGRAM["welcome_percent_off"],
            "referred_percent_off": REFERRAL_PROGRAM["referred_percent_off"],
            "referrer_percent_off_per_referral":
                REFERRAL_PROGRAM["referrer_percent_off_per_referral"],
            "referrer_percent_off_cap":
                REFERRAL_PROGRAM["referrer_percent_off_cap"],
            # Tuple in the source dict; JSON has no tuple, so serve a list.
            "eligible_intervals": list(REFERRAL_PROGRAM["eligible_intervals"]),
        },
    }


class CheckoutRequest(BaseModel):
    """Exactly one of tier / pack. Both are server-mapped to a price id — the
    client can NEVER supply a price id or amount. The free tier is not
    purchasable (it's the signup default). interval applies to tiers only:
    monthly = recurring subscription; season = one-time fixed-term entitlement."""
    tier: Optional[Literal["standard", "pro"]] = None
    interval: Literal["monthly", "season"] = "monthly"
    pack: Optional[str] = None  # validated against CREDIT_PACKS (source of truth)
    # Referral or welcome code. The code names a discount; the PERCENTAGE is
    # resolved server-side by ReferralService — the client never supplies an
    # amount here any more than it supplies a price id.
    code: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if bool(self.tier) == bool(self.pack):
            raise ValueError("Provide exactly one of 'tier' or 'pack'")
        if self.pack is not None and self.pack not in CREDIT_PACKS:
            raise ValueError(f"Unknown pack '{self.pack}'")
        # Normalize once, here, so every downstream read (validation, Stripe
        # metadata, the redemption row) sees the same string. An all-whitespace
        # code becomes None rather than an empty string that reads as "supplied".
        if self.code is not None:
            self.code = self.code.strip().upper() or None
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
    session = stripe_gateway.create_checkout_session(
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
    # Packs carry no discount, so there is nothing to reserve and the session id
    # is not needed here — the webhook keys the credit grant on it itself.
    return session.url


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
        # A code on a pack purchase is an error, not something to drop quietly.
        # Silently ignoring it would charge full price while the user believes a
        # discount was applied, and they would only find out on the receipt.
        if body.code:
            raise HTTPException(
                status_code=400,
                detail="Discount codes apply to subscription plans, not credit packs.",
            )
        return CheckoutResponse(url=_create_pack_session(user, customer_id, body.pack))

    price_id = catalog.tier_to_price(body.tier, body.interval)
    if not price_id:
        raise HTTPException(
            status_code=400,
            detail=f"No price configured for tier '{body.tier}' ({body.interval})",
        )

    metadata = {
        "tier": body.tier, "interval": body.interval,
        "user_id": str(user.id),
    }
    discounts = None
    referrals = None
    reservation_id = None
    if body.code:
        # ReferralService is the single judge — it enforces monthly-only,
        # self-referral, mutual referral, the welcome code's audience, and
        # one-per-kind. The coupon is chosen from the KIND it returns, so the
        # percentage on the Stripe coupon and the percentage we record both trace
        # back to REFERRAL_PROGRAM.
        from backend.services.referral_service import (
            CHECKOUT_IN_FLIGHT_MESSAGE,
            ReferralService,
        )

        referrals = ReferralService.from_session(db)
        resolved = await referrals.resolve_code(
            code=body.code,
            redeemer_user_id=user.id,
            interval=body.interval,
        )
        if not resolved.valid:
            raise HTTPException(status_code=400, detail=resolved.message)

        # Pick the coupon BEFORE the reservation exists. referral_coupon_id
        # raises ValueError on a kind it does not know, and raising it after the
        # reservation is committed but before the release handler below is
        # installed would leave the slot held by a checkout that was never
        # created. Nothing has been written yet at this line, so a raise here
        # costs nothing.
        discounts = [{"coupon": catalog.referral_coupon_id(resolved.kind)}]

        # RESERVE BEFORE STRIPE. Resolving alone cannot stop two tabs: both read
        # a clean slate, both get a session carrying the coupon, and both can be
        # paid. The reservation takes the account's one-per-kind slot at the
        # database, so the second attempt loses here — before a discounted
        # session exists to be paid. Doing it the other way round would leave a
        # live discounted session behind whenever the reservation lost.
        reservation_id = await referrals.reserve_for_checkout(
            resolved=resolved, redeemer_user_id=user.id, code=body.code
        )
        if reservation_id is None:
            # The same sentence resolve_code produces for an open checkout, from
            # the one definition, because it is the same situation: this account
            # has a payable discounted session already.
            raise HTTPException(status_code=400, detail=CHECKOUT_IN_FLIGHT_MESSAGE)

        # The webhook records the redemption from these fields — it cannot re-run
        # the lookup, because by then the code may have been reused or revoked.
        # Every Stripe metadata value must be a string, so the referrer id is an
        # empty string (not None, not omitted) when there is no referrer.
        metadata.update({
            "redeemed_code": body.code,
            "redeemed_kind": resolved.kind,
            "redeemed_percent_off": str(resolved.percent_off),
            "referrer_user_id": (
                str(resolved.referrer_user_id) if resolved.referrer_user_id else ""
            ),
        })

    # SEASON = one-time payment (mode=payment) granting the tier until the
    # season entitlement end; MONTHLY = recurring subscription. Proration/
    # change-plan applies only to subscriptions — season purchases go through
    # here in both directions (see change-plan notes).
    mode = "payment" if body.interval == "season" else "subscription"
    try:
        session = stripe_gateway.create_checkout_session(
            customer_id=customer_id,
            mode=mode,
            price_id=price_id,
            # These pages grant NOTHING (§0.B) — the webhook is the only grantor.
            success_url=f"{settings.app_url}/account?billing=success",
            cancel_url=f"{settings.app_url}/pricing?billing=cancel",
            metadata=metadata,
            discounts=discounts,
            # Fresh session per attempt (see _create_pack_session).
            idempotency_key=f"co_{user.id}_tier_{body.tier}_{body.interval}_{uuid.uuid4()}",
        )
        if reservation_id is not None:
            # Now the reservation can be found by the webhook: it looks the row
            # up by the Stripe session id on the completed event. INSIDE the try
            # because this is a database write and it can fail: a reservation
            # still carrying its provisional id can never be matched, so the
            # webhook would not find it and the slot would stay held for the full
            # pending TTL behind a discounted session that IS payable.
            await referrals.attach_checkout_session(reservation_id, session.id)
    except Exception:
        # Either Stripe never produced a session, or it did and we could not
        # point the reservation at it. Both leave a slot held for a checkout the
        # webhook can never match, so give it back.
        #
        # When the session DID get created, releasing is still the right move:
        # the discounted session stays payable, and the webhook's fallback path
        # records the redemption from the completed event's metadata, keyed on
        # the session id. The referrer is still paid, once.
        if reservation_id is not None:
            try:
                await referrals.release_reservation(reservation_id)
            except Exception:
                # Swallowed on purpose. Raising here would replace the real
                # failure — the Stripe error the caller needs to see — with a
                # database error from the cleanup, and would skip the `raise`
                # below entirely.
                logger.exception(
                    "Could not release referral reservation %s for user %s",
                    reservation_id, user.id,
                )
        raise

    return CheckoutResponse(url=session.url)


class ValidateCodeRequest(BaseModel):
    code: str
    interval: Literal["monthly", "season"] = "monthly"


class ValidateCodeResponse(BaseModel):
    valid: bool
    percent_off: int
    message: str


@router.post("/validate-code", response_model=ValidateCodeResponse)
async def validate_code(
    body: ValidateCodeRequest,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """Check a discount code without applying anything.

    Purely so the UI can show "30% off your first month" beside the input before
    the user is redirected to Stripe. It writes nothing and creates no session;
    /checkout re-resolves the code independently, so a code that goes stale
    between the two calls is still caught. Stripe need not be configured for this
    to answer — no Stripe call is made.
    """
    from backend.services.referral_service import ReferralService

    resolved = await ReferralService.from_session(db).resolve_code(
        code=body.code,
        redeemer_user_id=user.id,
        interval=body.interval,
    )
    return ValidateCodeResponse(
        valid=resolved.valid,
        percent_off=resolved.percent_off,
        message=resolved.message,
    )


class CheckoutPackRequest(BaseModel):
    pack: str  # validated against CREDIT_PACKS (source of truth)

    @model_validator(mode="after")
    def _known_pack(self):
        if self.pack not in CREDIT_PACKS:
            raise ValueError(f"Unknown pack '{self.pack}'")
        return self


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
    # free = downgrade-to-free (cancels the subscription at period end).
    target_tier: Literal["free", "standard", "pro"]


class ChangePlanConfirmRequest(BaseModel):
    target_tier: Literal["free", "standard", "pro"]
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
        # Season purchasers have an ENTITLEMENT, not a subscription — proration
        # cannot express season<->monthly, so plan changes for/to season go
        # through /billing/checkout (a fresh purchase; the webhook reconciles:
        # a season purchase cancels an active monthly at period end, a monthly
        # purchase clears the season expiry). This endpoint is monthly<->monthly.
        raise ValidationError(
            "No active monthly subscription to change — use checkout "
            "(season passes and new subscriptions are purchases, not plan changes)"
        )
    direction = catalog.is_upgrade(user.tier, target_tier)
    if direction is None:
        raise HTTPException(
            status_code=400, detail="Target tier must differ from the current tier"
        )
    # Free has no price — downgrading to it CANCELS the subscription at period end
    # (the webhook drops the tier to free when the sub actually ends). It is always
    # a downgrade, so the upgrade path never dereferences the None price. Every
    # other target maps to its monthly price.
    target_price = None
    if target_tier != "free":
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

    # Downgrade. To FREE = cancel the subscription at period end (there is no
    # target price to switch to); the webhook flips the tier to free when the sub
    # actually ends. To a lower PAID tier = schedule the price drop at period end.
    if body.target_tier == "free":
        stripe_gateway.cancel_at_period_end(
            sub_id=user.stripe_subscription_id,
            idempotency_key=f"chg_{user.id}_free_{snap['period_end']}",
        )
        effective_ts = snap["period_end"]
    else:
        result = stripe_gateway.schedule_downgrade(
            sub_id=user.stripe_subscription_id,
            current_price_id=snap["price_id"],
            target_price_id=target_price,
            idempotency_key=f"chg_{user.id}_{body.target_tier}_{snap['period_end']}",
        )
        effective_ts = result["effective"]
    effective = datetime.fromtimestamp(effective_ts, tz=timezone.utc).isoformat()
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
