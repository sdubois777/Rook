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
# Subscription configuration — single source of truth
# ---------------------------------------------------------------------------

TIER_LIMITS: dict[str, dict] = {
    "intro": {
        # $5/month or $15/season
        "credits_monthly": 0,         # no monthly reset
        "credits_signup_bonus": 25,   # one-time only
        "max_leagues": 1,
        "live_draft": False,
        "trade_analyzer": False,
        "trade_finder": False,
        "waiver_wire": False,
        "injury_monitoring": True,    # free for all tiers
    },
    "standard": {
        # $9/month or $29/season
        "credits_monthly": 20,
        "credits_signup_bonus": 75,
        "max_leagues": 2,
        "live_draft": True,
        "trade_analyzer": True,
        "trade_finder": False,        # Pro only
        "waiver_wire": True,
        "injury_monitoring": True,
    },
    "pro": {
        # $18/month or $49/season
        "credits_monthly": 50,
        "credits_signup_bonus": 200,
        "max_leagues": None,          # unlimited
        "live_draft": True,
        "trade_analyzer": True,
        "trade_finder": True,
        "waiver_wire": True,
        "injury_monitoring": True,
    },
}

# Credit costs per action.
# Feature access (tier) is checked BEFORE credits.
# Credits only deducted if feature is unlocked.
CREDIT_COSTS: dict[str, int] = {
    "trade_analysis": 10,   # ~$0.15 AI cost
    "trade_finder":   20,   # ~$0.50 AI cost (Pro only)
    "waiver_wire":     8,   # ~$0.05 AI cost
    # Live draft: tier entitlement — NOT a credit cost
    # Projections, draft board, news: always 0 (free)
}

# Credit purchase packs
CREDIT_PACKS: dict[str, dict] = {
    "small":  {"price_usd": 5,  "credits": 75},
    "medium": {"price_usd": 10, "credits": 175},
    "large":  {"price_usd": 25, "credits": 500},
}


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
        String(20), nullable=False, default="intro"
    )
    # "intro" | "standard" | "pro"

    # Credits
    credits_remaining: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Accumulate — never reset. Monthly credits ADD to balance.

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
