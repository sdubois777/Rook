"""Tests for backend/routers/billing.py — checkout, portal, change-plan, packs."""
from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


def _snap(now=None, price_id="price_standard"):
    now = now or int(time.time())
    return {
        "status": "active",
        "item_id": "si_1",
        "price_id": price_id,
        "period_start": now - 100_000,
        "period_end": now + 1_000_000,
    }


async def _post(path, json):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        return await ac.post(path, json=json)


def _make_user(customer_id="cus_1", tier="intro", subscription_id=None):
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.external_id = "user_abc"
    user.email = "u@example.com"
    user.tier = tier
    user.credits_remaining = 25
    user.stripe_customer_id = customer_id
    user.stripe_subscription_id = subscription_id
    return user


@pytest.fixture
def stripe_configured(monkeypatch):
    """Enable Stripe + deterministic price ids for the router under test."""
    from backend.config import settings
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_x", raising=False)
    monkeypatch.setattr(settings, "app_url", "http://localhost:8000", raising=False)
    monkeypatch.setattr(settings, "stripe_price_intro_monthly", "price_intro", raising=False)
    monkeypatch.setattr(settings, "stripe_price_standard_monthly", "price_standard", raising=False)
    monkeypatch.setattr(settings, "stripe_price_pro_monthly", "price_pro", raising=False)
    monkeypatch.setattr(settings, "stripe_price_pack_small", "price_small", raising=False)
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


# ── checkout-pack ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_checkout_pack_creates_payment_session(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway
    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: captured.update(kw) or "https://checkout.stripe.com/pack",
    )
    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/checkout-pack", {"pack": "small"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["mode"] == "payment"
    assert captured["price_id"] == "price_small"
    assert captured["metadata"]["credits"] == "75"


@pytest.mark.asyncio
async def test_checkout_pack_uses_fresh_idempotency_key_each_attempt(stripe_configured, monkeypatch):
    """A stable key would hand back a prior COMPLETED session ("you're all done
    here"); each purchase attempt must create a fresh Checkout session."""
    from backend.services.billing import stripe_gateway
    keys = []
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: keys.append(kw["idempotency_key"]) or "https://checkout.stripe.com/x",
    )
    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        await _post("/api/billing/checkout-pack", {"pack": "small"})
        await _post("/api/billing/checkout-pack", {"pack": "small"})
    finally:
        app.dependency_overrides.clear()

    assert len(keys) == 2
    assert keys[0] != keys[1]   # unique per attempt


# ── change-plan preview ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_plan_preview_upgrade(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(stripe_gateway, "subscription_snapshot", lambda sid: _snap())
    monkeypatch.setattr(
        "backend.repositories.league_repo.LeagueRepository.count_active",
        AsyncMock(return_value=1),
    )
    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "preview_upgrade_amount",
        lambda **kw: captured.update(kw) or 912,
    )
    user = _make_user(tier="standard", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/change-plan/preview", {"target_tier": "pro"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["direction"] == "upgrade"
    assert data["amount_due_today"] == 912
    assert data["effective"] == "now"
    assert isinstance(data["proration_date"], int)
    # server-mapped target price, never the client's
    assert captured["target_price_id"] == "price_pro"


@pytest.mark.asyncio
async def test_change_plan_preview_downgrade(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(
        stripe_gateway, "subscription_snapshot", lambda sid: _snap(price_id="price_pro")
    )
    monkeypatch.setattr(
        "backend.repositories.league_repo.LeagueRepository.count_active",
        AsyncMock(return_value=3),
    )
    user = _make_user(tier="pro", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/change-plan/preview", {"target_tier": "standard"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["direction"] == "downgrade"
    assert data["amount_due_today"] == 0
    assert data["proration_date"] is None
    assert data["effective"].startswith("20")  # ISO period-end date
    assert data["active_leagues"] == 3          # over the standard cap of 2
    assert data["max_active_leagues"] == 2


@pytest.mark.asyncio
async def test_change_plan_preview_same_tier_rejected(stripe_configured):
    user = _make_user(tier="pro", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/change-plan/preview", {"target_tier": "pro"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_change_plan_preview_no_active_sub_rejected(stripe_configured):
    user = _make_user(tier="standard", subscription_id=None)
    _override_auth(user)
    try:
        resp = await _post("/api/billing/change-plan/preview", {"target_tier": "pro"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422  # ValidationError — must subscribe first


# ── change-plan confirm ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_plan_confirm_upgrade_reuses_proration_date(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(stripe_gateway, "subscription_snapshot", lambda sid: _snap())
    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "apply_upgrade",
        lambda **kw: captured.update(kw) or {"status": "active"},
    )
    pd = int(time.time())
    user = _make_user(tier="standard", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/change-plan/confirm",
            {"target_tier": "pro", "proration_date": pd},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    # the SAME timestamp is reused, not regenerated
    assert captured["proration_date"] == pd
    assert captured["target_price_id"] == "price_pro"
    # confirm never writes users.tier (webhook is sole writer)
    assert user.tier == "standard"


@pytest.mark.asyncio
async def test_change_plan_confirm_upgrade_stale_proration_date_rejected(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(stripe_gateway, "subscription_snapshot", lambda sid: _snap())
    called = {"n": 0}
    monkeypatch.setattr(
        stripe_gateway, "apply_upgrade", lambda **kw: called.__setitem__("n", called["n"] + 1)
    )
    future = int(time.time()) + 100_000  # far future → free-upgrade exploit
    user = _make_user(tier="standard", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/change-plan/confirm",
            {"target_tier": "pro", "proration_date": future},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert called["n"] == 0  # never touched Stripe


@pytest.mark.asyncio
async def test_change_plan_confirm_downgrade_schedules(stripe_configured, monkeypatch):
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(
        stripe_gateway, "subscription_snapshot", lambda sid: _snap(price_id="price_pro")
    )
    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "schedule_downgrade",
        lambda **kw: captured.update(kw) or {"schedule_id": "sub_sched_1", "effective": int(time.time()) + 1_000_000},
    )
    user = _make_user(tier="pro", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/change-plan/confirm", {"target_tier": "standard"}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["status"] == "scheduled"
    assert captured["current_price_id"] == "price_pro"
    assert captured["target_price_id"] == "price_standard"
    assert user.tier == "pro"  # unchanged until the schedule advances
