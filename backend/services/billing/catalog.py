"""
Price catalog — the ONLY place price ids are mapped to tiers / pack credits.

Server-authoritative by design (§0.B): the checkout endpoint accepts a *tier
name* or *pack name*, never a client-supplied price id or amount, and the webhook
resolves a subscription's price id back to a tier here. Nothing is hardcoded in the
handlers — it all reads from `settings` (env-populated, test/live agnostic).

Credit-pack amounts come from `CREDIT_PACKS` (the source of truth in models/user.py),
never re-declared here.
"""
from __future__ import annotations

from typing import Optional

from backend.config import Settings, settings
from backend.models.user import CREDIT_PACKS

# Tier <-> monthly Price. Order matters only for readability.
_TIER_PRICE_ATTR = {
    "intro": "stripe_price_intro_monthly",
    "standard": "stripe_price_standard_monthly",
    "pro": "stripe_price_pro_monthly",
}

# Pack <-> one-time Price.
_PACK_PRICE_ATTR = {
    "small": "stripe_price_pack_small",
    "medium": "stripe_price_pack_medium",
    "large": "stripe_price_pack_large",
}


def tier_to_price(tier: str, s: Settings = settings) -> Optional[str]:
    """Server-configured monthly price id for a tier, or None if unconfigured."""
    attr = _TIER_PRICE_ATTR.get(tier)
    return getattr(s, attr, None) if attr else None


def price_to_tier(price_id: str, s: Settings = settings) -> Optional[str]:
    """Map a subscription's price id back to its tier (webhook path).

    Returns None for an unknown/unconfigured price id — the caller treats that as
    'no tier change' rather than guessing.
    """
    if not price_id:
        return None
    for tier, attr in _TIER_PRICE_ATTR.items():
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
