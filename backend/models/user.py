"""
User model and tier configuration.

TIER_LIMITS and CREDIT_COSTS are the single source
of truth for subscription rules.
No other file should define these values.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID

from backend.database import Base


# ---------------------------------------------------------------------------
# Subscription configuration — THE SINGLE SOURCE OF TRUTH
#
# Every price, credit cost, grant size, pack size, and entitlement is defined
# HERE and ONLY here. The Stripe seeder, the billing catalog, the public
# /billing/pricing endpoint (which the frontend renders from), and the docs all
# DERIVE from these dicts. Defining any of these numbers anywhere else is the
# four-way pricing drift this file exists to prevent.
#
# MODEL (the gate-semantics flip):
#   * FREE  — metered: every metered feature is usable by SPENDING CREDITS
#             (CREDIT_COSTS). One-time 30-credit signup grant. No live draft.
#   * PAID  — unlimited_features=True: metered features run with NO debit and
#             no credit check. Live draft included.
#   * Tier-ENTITLEMENT features (live_draft, cross_league_view) stay binary
#     403 gates — never routed through credits (stranding a user at zero
#     credits mid-draft is a product catastrophe for pennies).
#   * ALWAYS FREE for everyone, never metered: player values, teams, player
#     detail, waiver WIRE BROWSE, start/sit, injury revaluation (pipeline-
#     shared — gating it would serve stale values, i.e. a WRONG product).
# ---------------------------------------------------------------------------

# Ordered lowest → highest; drives rank comparisons everywhere.
TIER_ORDER: tuple[str, ...] = ("free", "standard", "pro")

TIER_LIMITS: dict[str, dict] = {
    "free": {
        "label": "Free",
        "price_monthly_usd": 0,
        "price_season_usd": 0,
        "credits_signup_bonus": 30,     # one-time, granted at signup
        "max_leagues": 1,
        "unlimited_features": False,    # metered — features cost credits
        "live_draft": False,
        "cross_league_view": False,
        "injury_monitoring": True,      # always free, all tiers
    },
    "standard": {
        "label": "Standard",
        "price_monthly_usd": 8,
        "price_season_usd": 29,
        "credits_signup_bonus": 0,      # unlimited — credits irrelevant
        "max_leagues": 1,
        "unlimited_features": True,
        "live_draft": True,
        "cross_league_view": False,
        "injury_monitoring": True,
    },
    "pro": {
        "label": "Pro",
        "price_monthly_usd": 18,
        "price_season_usd": 59,
        "credits_signup_bonus": 0,
        "max_leagues": None,            # unlimited leagues
        "unlimited_features": True,
        "live_draft": True,
        "cross_league_view": True,
        "injury_monitoring": True,
    },
}

# Credit costs per metered action — FREE TIER ONLY (paid tiers never debit).
# START/SIT IS DELIBERATELY ABSENT: it auto-fires inside GET /matchup/league on
# page mount, is pure Python ($0), and metering it would mean a silent debit on
# NAVIGATION. It stays free (decided).
CREDIT_COSTS: dict[str, int] = {
    "trade_analysis": 1,
    "waiver_wire":    2,   # waiver RECOMMENDATIONS (the browse list is free)
    "trade_finder":   5,
    # Live draft: tier entitlement — NOT a credit cost, never metered.
}

# Credit top-up packs. Add a pack here and everything else derives — config env
# field (stripe_price_pack_<credits>), catalog attr map, seeder price, checkout
# validation, public /pricing sheet, and the frontend BuyCreditsCard all read this.
CREDIT_PACKS: dict[str, dict] = {
    "credits_100": {"price_usd": 5,  "credits": 100},
    "credits_200": {"price_usd": 9,  "credits": 200},
    "credits_500": {"price_usd": 25, "credits": 500},
}

# ---------------------------------------------------------------------------
# Referral program — percentages live HERE and nowhere else.
#
# Same anti-drift rule as the pricing dicts above: the Stripe coupon seeder, the
# referral service, the public /billing/pricing sheet, the email templates, and
# the frontend copy all DERIVE from this dict. A percentage written into a
# template string or a React component is the drift this block exists to stop.
#
# THREE DISCOUNTS, three different audiences:
#   * welcome_percent_off      — emailed to a free signup who has not paid. A
#                                conversion nudge, redeemable whenever they
#                                subscribe. One per user, ever.
#   * referred_percent_off     — a NEW customer who checks out with someone
#                                else's referral code. One per user, ever.
#   * referrer_percent_off_*   — the reward the REFERRER earns. Each confirmed
#                                referral adds `per_referral`, stacking up to
#                                `cap`. Applied as ONE recurring coupon at the
#                                summed rate (not N stacked coupons — Stripe
#                                stacking semantics are ambiguous and a
#                                per-referral coupon cannot be reversed cleanly
#                                when a referral is refunded).
#
# MONTHLY ONLY (decided). Season passes ($29/$59) are one-time payments with no
# recurring invoice, so a recurring referrer discount has nothing to attach to
# and "30% off the first month" is undefined. Both discount paths refuse a
# season checkout rather than silently applying to the full season price. Flip
# `eligible_intervals` to ("monthly", "season") to change that — but read
# docs/referral_and_email_design.md first, because the referrer reward needs a
# different mechanism for one-time payments.
# ---------------------------------------------------------------------------

REFERRAL_PROGRAM: dict = {
    "welcome_percent_off": 20,
    "referred_percent_off": 30,
    "referrer_percent_off_per_referral": 10,
    "referrer_percent_off_cap": 50,          # 5 referrals × 10%
    "eligible_intervals": ("monthly",),
    # Code shown to the user and typed by their friend. Ambiguous characters
    # (0/O, 1/I/L) are excluded — these get read aloud and retyped from memory.
    "code_prefix": "ROOK",
    "code_alphabet": "23456789ABCDEFGHJKMNPQRSTUVWXYZ",
    "code_length": 6,
}


def referrer_percent_off(referral_count: int) -> int:
    """The referrer's total discount rate for `referral_count` confirmed referrals.

    Capped, and floored at 0 so a reversal that drives the count negative (it
    shouldn't, but a reversal race could) can never produce a negative coupon.
    """
    per = REFERRAL_PROGRAM["referrer_percent_off_per_referral"]
    cap = REFERRAL_PROGRAM["referrer_percent_off_cap"]
    return max(0, min(cap, per * max(0, referral_count)))


def referral_percent_tiers() -> tuple[int, ...]:
    """Every referrer discount rate the program can produce, ascending.

    The coupon seeder creates exactly one Stripe coupon per rate, so this is
    what bounds the number of coupon objects (5 at 10/20/30/40/50).
    """
    per = REFERRAL_PROGRAM["referrer_percent_off_per_referral"]
    cap = REFERRAL_PROGRAM["referrer_percent_off_cap"]
    return tuple(range(per, cap + 1, per))


def interval_is_referral_eligible(interval: str) -> bool:
    """True when a referral/welcome discount may be applied to this interval."""
    return interval in REFERRAL_PROGRAM["eligible_intervals"]


def is_unlimited(tier: str) -> bool:
    """True for paid tiers — metered features run with no debit at all."""
    return bool(TIER_LIMITS.get(tier, {}).get("unlimited_features", False))


def effective_tier(user: "User") -> str:
    """The tier that ACTUALLY applies right now.

    Season purchases are one-time entitlements with an expiry
    (``tier_expires_at``); past it, the user is effectively free until the
    lazy write-back (or the next purchase) lands. Monthly subscriptions have
    no expiry (NULL) — Stripe's subscription.deleted webhook downgrades them.
    """
    from datetime import datetime, timezone

    tier = user.tier
    expires = getattr(user, "tier_expires_at", None)
    if tier in ("standard", "pro") and expires is not None:
        if datetime.now(timezone.utc) >= expires:
            return "free"
    return tier


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    # external_id = Clerk user ID (e.g. "user_2NF...")

    email: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    display_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )

    # Subscription
    tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default="free"
    )
    # "free" | "standard" | "pro"

    # Season-purchase expiry: set ONLY for one-time season entitlements
    # (tier holds until this instant, then effectively free). NULL for monthly
    # subscriptions (Stripe's subscription.deleted is their downgrade) and for
    # the free tier. Read via effective_tier().
    tier_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Credits
    credits_remaining: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Accumulate — never reset. Spent only by the FREE tier on metered actions.

    # Browser extension auth — long-lived UUID token
    draft_token: Mapped[Optional[str]] = mapped_column(
        String(36), unique=True, nullable=True, index=True
    )

    # Stripe
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(
        String(100), unique=True, nullable=True
    )
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(
        String(100), unique=True, nullable=True
    )
    subscription_status: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )
    # None (no subscription) | "active" | "past_due" | "canceling".
    # Written only by the verified Stripe webhook. NOT an entitlement source —
    # tier remains the source of truth; this is billing state for the UI.

    # Lifecycle
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Soft delete — keep data for billing/legal

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Credit usage log
# ---------------------------------------------------------------------------

class CreditUsageLog(Base):
    __tablename__ = "credit_usage_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        # ForeignKey added via migration — avoids circular import
        nullable=False, index=True,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    credits_used: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_name: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    cost_usd: Mapped[Optional[float]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
