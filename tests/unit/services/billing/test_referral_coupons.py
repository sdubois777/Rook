"""Tests for the referral coupon ids in backend/services/billing/catalog.py.

The ids are derived, not configured, so scripts/stripe_seed_referral_coupons.py
and the runtime agree without a lookup table. These tests pin the scheme, because
a change to it silently stops the runtime from finding the seeded coupons.
"""
from __future__ import annotations

import pytest

from backend.models.referral import KIND_REFERRAL, KIND_WELCOME
from backend.models.user import REFERRAL_PROGRAM, referral_percent_tiers
from backend.services.billing.catalog import referral_coupon_id, referrer_coupon_id


def test_one_time_coupon_ids_follow_the_documented_scheme():
    assert referral_coupon_id(KIND_WELCOME) == (
        f"rook_once_{REFERRAL_PROGRAM['welcome_percent_off']}"
    )
    assert referral_coupon_id(KIND_REFERRAL) == (
        f"rook_once_{REFERRAL_PROGRAM['referred_percent_off']}"
    )


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        referral_coupon_id("bonus")


def test_referrer_coupon_ids_exist_for_every_reachable_rate():
    for percent in referral_percent_tiers():
        assert referrer_coupon_id(percent) == f"rook_forever_{percent}"


def test_referrer_coupon_id_is_none_at_zero():
    """There is no zero-percent coupon object — the caller clears the
    subscription's discounts instead."""
    assert referrer_coupon_id(0) is None
    assert referrer_coupon_id(-10) is None


def test_one_time_and_recurring_ids_never_collide():
    once = {referral_coupon_id(KIND_WELCOME), referral_coupon_id(KIND_REFERRAL)}
    forever = {referrer_coupon_id(p) for p in referral_percent_tiers()}
    assert not once & forever


def test_every_runtime_coupon_id_is_one_the_seeder_creates():
    """The drift this catches is silent in production: if the runtime asks for a
    coupon id the seeder never created, Stripe rejects it, the discount does not
    apply, and the customer pays full price with no error on our side."""
    from scripts.stripe_seed_referral_coupons import coupon_plan

    seeded = {entry["id"] for entry in coupon_plan()}
    runtime = {
        referral_coupon_id(KIND_WELCOME),
        referral_coupon_id(KIND_REFERRAL),
        *(referrer_coupon_id(p) for p in referral_percent_tiers()),
    }

    assert runtime <= seeded
