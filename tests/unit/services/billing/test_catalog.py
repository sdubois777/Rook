"""Tests for the Stripe price catalog (price <-> tier / pack-credits resolver)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.billing import catalog


def _settings(**overrides):
    base = dict(
        stripe_price_intro_monthly="price_intro",
        stripe_price_standard_monthly="price_standard",
        stripe_price_pro_monthly="price_pro",
        stripe_price_pack_small="price_small",
        stripe_price_pack_medium="price_medium",
        stripe_price_pack_large="price_large",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_tier_to_price_round_trips():
    s = _settings()
    assert catalog.tier_to_price("intro", s) == "price_intro"
    assert catalog.tier_to_price("standard", s) == "price_standard"
    assert catalog.tier_to_price("pro", s) == "price_pro"


def test_price_to_tier_round_trips():
    s = _settings()
    assert catalog.price_to_tier("price_intro", s) == "intro"
    assert catalog.price_to_tier("price_standard", s) == "standard"
    assert catalog.price_to_tier("price_pro", s) == "pro"


def test_price_to_tier_unknown_returns_none():
    s = _settings()
    assert catalog.price_to_tier("price_nonexistent", s) is None
    assert catalog.price_to_tier("", s) is None


def test_price_to_tier_ignores_unconfigured_none():
    # An unconfigured (None) price id must never match a None lookup.
    s = _settings(stripe_price_pro_monthly=None)
    assert catalog.price_to_tier(None, s) is None
    assert catalog.tier_to_price("pro", s) is None


def test_pack_price_and_credits():
    s = _settings()
    assert catalog.pack_to_price("small", s) == "price_small"
    assert catalog.pack_to_price("medium", s) == "price_medium"
    assert catalog.pack_to_price("large", s) == "price_large"


def test_pack_to_credits_from_source_of_truth():
    # From CREDIT_PACKS in models/user.py — never redeclared in the catalog.
    assert catalog.pack_to_credits("small") == 75
    assert catalog.pack_to_credits("medium") == 175
    assert catalog.pack_to_credits("large") == 500
    assert catalog.pack_to_credits("bogus") is None


def test_price_to_pack_credits_round_trips():
    s = _settings()
    assert catalog.price_to_pack_credits("price_small", s) == 75
    assert catalog.price_to_pack_credits("price_medium", s) == 175
    assert catalog.price_to_pack_credits("price_large", s) == 500
    assert catalog.price_to_pack_credits("price_unknown", s) is None
