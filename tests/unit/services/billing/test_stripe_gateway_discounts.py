"""Tests for the discount parameters in backend/services/billing/stripe_gateway.py.

The Stripe SDK is monkeypatched — nothing here makes a network call. What is
under test is which parameters reach Stripe, because Stripe rejects `discounts`
when it is present but empty, and rejects it outright alongside
allow_promotion_codes.
"""
from __future__ import annotations

import pytest
import stripe

from backend.services.billing import stripe_gateway


@pytest.fixture
def stripe_key(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)


@pytest.fixture
def captured_session(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        stripe.checkout.Session,
        "create",
        staticmethod(
            lambda **kw: captured.update(kw)
            or {"id": "cs_test_1", "url": "https://checkout.stripe.com/x"}
        ),
    )
    return captured


def _create(**overrides):
    args = dict(
        customer_id="cus_1",
        mode="subscription",
        price_id="price_standard",
        success_url="https://app/ok",
        cancel_url="https://app/no",
        metadata={"user_id": "u1"},
        idempotency_key="co_1",
    )
    args.update(overrides)
    return stripe_gateway.create_checkout_session(**args)


def test_checkout_omits_discounts_when_not_supplied(stripe_key, captured_session):
    _create()
    assert "discounts" not in captured_session


def test_checkout_omits_discounts_when_empty(stripe_key, captured_session):
    """An empty list is an API error, not a no-op."""
    _create(discounts=[])
    assert "discounts" not in captured_session


def test_checkout_forwards_a_coupon(stripe_key, captured_session):
    _create(discounts=[{"coupon": "rook_once_30"}])
    assert captured_session["discounts"] == [{"coupon": "rook_once_30"}]
    # allow_promotion_codes is mutually exclusive with discounts and is never set.
    assert "allow_promotion_codes" not in captured_session


def test_checkout_returns_the_session_id_and_url(stripe_key, captured_session):
    """The id is what the referral reservation and the webhook agree on, so it
    has to come back alongside the redirect URL."""
    session = _create()

    assert session.id == "cs_test_1"
    assert session.url == "https://checkout.stripe.com/x"


@pytest.fixture
def captured_modify(monkeypatch):
    captured = {}

    def fake_modify(sub_id, **kw):
        captured["sub_id"] = sub_id
        captured.update(kw)
        return {"status": "active"}

    monkeypatch.setattr(stripe.Subscription, "modify", staticmethod(fake_modify))
    return captured


def test_set_subscription_discount_applies_one_coupon(stripe_key, captured_modify):
    """One coupon at the summed rate — the list REPLACES whatever was there, so
    raising the reward is a single call rather than a second stacked coupon."""
    stripe_gateway.set_subscription_discount(
        sub_id="sub_1", coupon_id="rook_forever_30", idempotency_key="k1"
    )

    assert captured_modify["sub_id"] == "sub_1"
    assert captured_modify["discounts"] == [{"coupon": "rook_forever_30"}]
    assert captured_modify["idempotency_key"] == "k1"


def test_set_subscription_discount_clears_with_an_empty_list(stripe_key, captured_modify):
    """coupon_id=None removes every discount — used when a reversal drops the
    referral count back to zero."""
    stripe_gateway.set_subscription_discount(
        sub_id="sub_1", coupon_id=None, idempotency_key="k2"
    )

    assert captured_modify["discounts"] == []


def test_gateway_raises_without_a_configured_key(monkeypatch, captured_session):
    from backend.config import settings
    monkeypatch.setattr(settings, "stripe_secret_key", None, raising=False)

    with pytest.raises(RuntimeError):
        _create()
