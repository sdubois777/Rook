"""Tests for backend/routers/billing.py — checkout + portal session creation."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


def _make_user(customer_id="cus_1"):
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.external_id = "user_abc"
    user.email = "u@example.com"
    user.tier = "intro"
    user.credits_remaining = 25
    user.stripe_customer_id = customer_id
    user.stripe_subscription_id = None
    return user


@pytest.fixture
def stripe_configured(monkeypatch):
    """Enable Stripe + deterministic price ids for the router under test."""
    from backend.config import settings
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "app_url", "http://localhost:8000", raising=False)
    monkeypatch.setattr(settings, "stripe_price_standard_monthly", "price_standard", raising=False)
    monkeypatch.setattr(settings, "stripe_price_pack_medium", "price_medium", raising=False)


def _override_auth(user):
    from backend.core.dependencies import get_current_user, get_db
    from backend.middleware.rate_limit import rate_limit_auth
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    app.dependency_overrides[rate_limit_auth] = lambda: None


@pytest.mark.asyncio
async def test_checkout_subscription_uses_server_price_and_bound_customer(
    stripe_configured, monkeypatch
):
    from backend.services.billing import stripe_gateway

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return "https://checkout.stripe.com/c/test_session"

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", fake_create)

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            # Client tries to smuggle a price_id + customer_id — both must be ignored.
            resp = await ac.post(
                "/api/billing/checkout",
                json={
                    "tier": "standard",
                    "price_id": "price_HACK",
                    "customer_id": "cus_HACK",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://checkout.stripe.com/")
    # Server-mapped price + server-bound customer — never the client's values.
    assert captured["price_id"] == "price_standard"
    assert captured["customer_id"] == "cus_1"
    assert captured["mode"] == "subscription"
    assert captured["metadata"]["tier"] == "standard"
    # The success page grants nothing — just the SPA account view.
    assert captured["success_url"] == "http://localhost:8000/account?billing=success"


@pytest.mark.asyncio
async def test_checkout_pack_uses_payment_mode(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway

    captured = {}
    monkeypatch.setattr(
        stripe_gateway,
        "create_checkout_session",
        lambda **kw: captured.update(kw) or "https://checkout.stripe.com/c/x",
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/billing/checkout", json={"pack": "medium"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["mode"] == "payment"
    assert captured["price_id"] == "price_medium"
    assert captured["metadata"]["credits"] == "175"


@pytest.mark.asyncio
async def test_checkout_rejects_both_tier_and_pack(stripe_configured):
    user = _make_user()
    _override_auth(user)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/billing/checkout",
                json={"tier": "standard", "pack": "medium"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_checkout_503_when_stripe_not_configured(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "stripe_secret_key", None, raising=False)

    user = _make_user()
    _override_auth(user)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/billing/checkout", json={"tier": "standard"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_portal_returns_url(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway

    captured = {}
    monkeypatch.setattr(
        stripe_gateway,
        "create_portal_session",
        lambda **kw: captured.update(kw) or "https://billing.stripe.com/p/test",
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/billing/portal")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://billing.stripe.com/")
    assert captured["customer_id"] == "cus_1"


@pytest.mark.asyncio
async def test_portal_422_without_customer(stripe_configured):
    user = _make_user(customer_id=None)
    _override_auth(user)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/billing/portal")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422
