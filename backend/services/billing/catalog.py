"""
Price catalog — the ONLY place price ids are mapped to tiers / intervals / packs.

Server-authoritative by design (§0.B): the checkout endpoint accepts a *tier
name* (+ interval) or *pack name*, never a client-supplied price id or amount,
and the webhook resolves a subscription's price id back to a tier here. Nothing
is hardcoded in the handlers — it all reads from `settings` (env-populated,
test/live agnostic).

Dollar amounts, credit amounts, and tier ordering are NEVER re-declared here —
they come from backend/models/user.py (THE source of truth).

INTERVALS: "monthly" = recurring Stripe subscription. "season" = ONE-TIME
payment granting the tier until the season end (users.tier_expires_at) — not a
subscription, so proration/change-plan don't apply to it (see billing router).
"""
from __future__ import annotations

from typing import Optional

from backend.config import Settings, settings
from backend.models.user import CREDIT_PACKS, TIER_ORDER

INTERVALS = ("monthly", "season")

# (tier, interval) <-> Price env attr. The free tier has no price by definition.
_TIER_PRICE_ATTR = {
    ("standard", "monthly"): "stripe_price_standard_monthly",
    ("standard", "season"):  "stripe_price_standard_season",
    ("pro", "monthly"):      "stripe_price_pro_monthly",
    ("pro", "season"):       "stripe_price_pro_season",
}

# Pack <-> one-time Price. Both the pack names AND the env-attr naming convention
# (stripe_price_pack_<credits>) derive from CREDIT_PACKS — add a pack in user.py
# and this map grows automatically; only the config field needs declaring.
_PACK_PRICE_ATTR = {
    name: f"stripe_price_pack_{cfg['credits']}"
    for name, cfg in CREDIT_PACKS.items()
}

# Tier ordering derives from user.py's TIER_ORDER — never re-declared.
_TIER_RANK = {tier: i for i, tier in enumerate(TIER_ORDER)}


def tier_rank(tier: str) -> Optional[int]:
    """Ordinal rank of a tier (free < standard < pro), or None if unknown."""
    return _TIER_RANK.get(tier)


def is_upgrade(current_tier: str, target_tier: str) -> Optional[bool]:
    """True if target outranks current (upgrade), False if lower (downgrade),
    None if either tier is unknown or they're equal."""
    a, b = tier_rank(current_tier), tier_rank(target_tier)
    if a is None or b is None or a == b:
        return None
    return b > a


def tier_to_price(
    tier: str, interval: str = "monthly", s: Settings = settings,
) -> Optional[str]:
    """Server-configured price id for a (tier, interval), or None."""
    attr = _TIER_PRICE_ATTR.get((tier, interval))
    return getattr(s, attr, None) if attr else None


def price_to_tier(price_id: str, s: Settings = settings) -> Optional[str]:
    """Map a price id back to its tier (webhook path). Interval-agnostic —
    subscription events only ever carry monthly prices (season is a one-time
    payment, never a subscription), but resolving either is harmless.

    Returns None for an unknown/unconfigured price id — the caller treats that
    as 'no tier change' rather than guessing.
    """
    if not price_id:
        return None
    for (tier, _interval), attr in _TIER_PRICE_ATTR.items():
        configured = getattr(s, attr, None)
        if configured and configured == price_id:
            return tier
    return None


def pack_to_price(pack: str, s: Settings = settings) -> Optional[str]:
    """Server-configured one-time price id for a credit pack, or None."""
    attr = _PACK_PRICE_ATTR.get(pack)
    return getattr(s, attr, None) if attr else None


def pack_to_credits(pack: str) -> Optional[int]:
    """Credit amount for a pack, from CREDIT_PACKS (source of truth)."""
    entry = CREDIT_PACKS.get(pack)
    return entry["credits"] if entry else None


def price_to_pack_credits(price_id: str, s: Settings = settings) -> Optional[int]:
    """Map a one-time pack price id back to its credit amount (webhook path)."""
    if not price_id:
        return None
    for pack, attr in _PACK_PRICE_ATTR.items():
        configured = getattr(s, attr, None)
        if configured and configured == price_id:
            return pack_to_credits(pack)
    return None
