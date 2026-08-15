"""Tests for backend/routers/billing.py — checkout, portal, change-plan, packs."""
from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


# Sentinel: "let the helper mint a reservation id", so that passing None can mean
# "the reservation was refused" without colliding with the default.
_NEW = object()


def _session(url="https://checkout.stripe.com/c/x", session_id="cs_test_1"):
    """What stripe_gateway.create_checkout_session hands the router back.

    Both fields matter: the url is returned to the client, and the id is what the
    referral reservation is re-keyed on so the webhook can find it.
    """
    from backend.services.billing.stripe_gateway import CheckoutSession

    return CheckoutSession(id=session_id, url=url)


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


def _make_user(customer_id="cus_1", tier="free", subscription_id=None):
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.external_id = "user_abc"
    user.email = "u@example.com"
    user.tier = tier
    user.tier_expires_at = None
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
    monkeypatch.setattr(settings, "stripe_price_standard_monthly", "price_standard", raising=False)
    monkeypatch.setattr(settings, "stripe_price_standard_season", "price_standard_s", raising=False)
    monkeypatch.setattr(settings, "stripe_price_pro_monthly", "price_pro", raising=False)
    monkeypatch.setattr(settings, "stripe_price_pro_season", "price_pro_s", raising=False)
    monkeypatch.setattr(settings, "stripe_price_pack_100", "price_pack", raising=False)


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
        return _session("https://checkout.stripe.com/c/test_session")

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
        lambda **kw: captured.update(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/billing/checkout", json={"pack": "credits_100"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["mode"] == "payment"
    assert captured["price_id"] == "price_pack"
    assert captured["metadata"]["credits"] == "100"


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
        lambda **kw: captured.update(kw) or _session("https://checkout.stripe.com/pack"),
    )
    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/checkout-pack", {"pack": "credits_100"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["mode"] == "payment"
    assert captured["price_id"] == "price_pack"
    assert captured["metadata"]["credits"] == "100"


@pytest.mark.asyncio
async def test_checkout_pack_uses_fresh_idempotency_key_each_attempt(stripe_configured, monkeypatch):
    """A stable key would hand back a prior COMPLETED session ("you're all done
    here"); each purchase attempt must create a fresh Checkout session."""
    from backend.services.billing import stripe_gateway
    keys = []
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: keys.append(kw["idempotency_key"]) or _session(),
    )
    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        await _post("/api/billing/checkout-pack", {"pack": "credits_100"})
        await _post("/api/billing/checkout-pack", {"pack": "credits_100"})
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
    assert data["active_leagues"] == 3          # over the standard cap of 1
    assert data["max_active_leagues"] == 1


@pytest.mark.asyncio
async def test_change_plan_preview_downgrade_to_free(stripe_configured, monkeypatch):
    """Free is a valid downgrade target: no price lookup, no charge, period-end."""
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(
        stripe_gateway, "subscription_snapshot", lambda sid: _snap(price_id="price_pro")
    )
    monkeypatch.setattr(
        "backend.repositories.league_repo.LeagueRepository.count_active",
        AsyncMock(return_value=1),
    )
    user = _make_user(tier="pro", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/change-plan/preview", {"target_tier": "free"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["direction"] == "downgrade"
    assert data["amount_due_today"] == 0
    assert data["target_tier"] == "free"
    assert data["effective"].startswith("20")


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


@pytest.mark.asyncio
async def test_change_plan_confirm_downgrade_to_free_cancels(stripe_configured, monkeypatch):
    """Downgrade to free cancels the sub at period end — no price schedule; the
    webhook drops the tier to free when the sub actually ends."""
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(
        stripe_gateway, "subscription_snapshot", lambda sid: _snap(price_id="price_pro")
    )
    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "cancel_at_period_end", lambda **kw: captured.update(kw)
    )
    monkeypatch.setattr(
        stripe_gateway, "schedule_downgrade",
        lambda **kw: (_ for _ in ()).throw(AssertionError("free must not schedule a price")),
    )
    user = _make_user(tier="pro", subscription_id="sub_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/change-plan/confirm", {"target_tier": "free"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "scheduled"
    assert body["target_tier"] == "free"
    assert captured["sub_id"] == "sub_1"
    assert user.tier == "pro"  # webhook is the sole tier-writer, on sub end


# ── referral / welcome codes at checkout ────────────────────────────────

def _fake_resolve(monkeypatch, resolved, *, reservation_id=_NEW):
    """Replace the ReferralService methods /checkout drives with fakes.

    resolve_code always returns `resolved`; the reservation methods record what
    they were asked to do. `reservation_id=None` simulates losing the race with a
    concurrent checkout. Everything the endpoint did is captured in one dict so a
    test can assert on the order of operations.
    """
    from backend.services.referral_service import ReferralService

    captured = {"reserved": [], "attached": [], "released": []}
    if reservation_id is _NEW:
        reservation_id = uuid.uuid4()

    async def fake_resolve(self, *, code, redeemer_user_id, interval):
        captured.update(
            code=code, redeemer_user_id=redeemer_user_id, interval=interval
        )
        return resolved

    async def fake_reserve(self, *, resolved, redeemer_user_id, code):
        captured["reserved"].append((redeemer_user_id, code, resolved.kind))
        return reservation_id

    async def fake_attach(self, reservation, stripe_session_id):
        captured["attached"].append((reservation, stripe_session_id))

    async def fake_release(self, reservation):
        captured["released"].append(reservation)

    monkeypatch.setattr(ReferralService, "resolve_code", fake_resolve)
    monkeypatch.setattr(ReferralService, "reserve_for_checkout", fake_reserve)
    monkeypatch.setattr(ReferralService, "attach_checkout_session", fake_attach)
    monkeypatch.setattr(ReferralService, "release_reservation", fake_release)
    captured["reservation_id"] = reservation_id
    return captured


def _resolved(**overrides):
    from backend.services.referral_service import ResolvedCode

    fields = dict(
        valid=True, kind="referral", percent_off=30,
        referrer_user_id=uuid.uuid4(), message="30% off your first month.",
    )
    fields.update(overrides)
    return ResolvedCode(**fields)


@pytest.mark.asyncio
async def test_checkout_with_referral_code_applies_coupon_and_metadata(
    stripe_configured, monkeypatch
):
    """The coupon comes from the resolved KIND (server-side), and the referrer id
    rides in metadata so the webhook can pay the reward."""
    from backend.services.billing import catalog, stripe_gateway

    resolved = _resolved()
    _fake_resolve(monkeypatch, resolved)
    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: captured.update(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/checkout",
            {"tier": "standard", "interval": "monthly", "code": "rook-friend"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["discounts"] == [
        {"coupon": catalog.referral_coupon_id("referral")}
    ]
    meta = captured["metadata"]
    assert meta["redeemed_code"] == "ROOK-FRIEND"      # normalized before use
    assert meta["redeemed_kind"] == "referral"
    assert meta["redeemed_percent_off"] == "30"
    assert meta["referrer_user_id"] == str(resolved.referrer_user_id)
    # Every Stripe metadata value must be a string.
    assert all(isinstance(v, str) for v in meta.values())


@pytest.mark.asyncio
async def test_checkout_with_welcome_code_sends_empty_referrer_id(
    stripe_configured, monkeypatch
):
    """Nobody earns a reward for a welcome code, and Stripe metadata cannot hold
    a null — so the field is present and empty, never omitted."""
    from backend.services.billing import catalog, stripe_gateway

    _fake_resolve(
        monkeypatch,
        _resolved(kind="welcome", percent_off=20, referrer_user_id=None),
    )
    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: captured.update(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        from backend.services.referral_service import ReferralService

        resp = await _post(
            "/api/billing/checkout",
            # A welcome code is per-user and derived from the user id — there is
            # no shared string to post here.
            {
                "tier": "standard",
                "code": ReferralService(None, None).welcome_code_for(user.id),
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["metadata"]["referrer_user_id"] == ""
    assert captured["discounts"] == [
        {"coupon": catalog.referral_coupon_id("welcome")}
    ]


@pytest.mark.asyncio
async def test_checkout_reserves_the_discount_before_calling_stripe(
    stripe_configured, monkeypatch
):
    """The reservation is what stops two tabs both receiving a paid discount, so
    it has to be taken before a discounted session exists to be paid. Afterwards
    the row is re-keyed on the real session id — that is how the webhook finds
    it."""
    from backend.services.billing import stripe_gateway

    captured = _fake_resolve(monkeypatch, _resolved())
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: _session(session_id="cs_live_9"),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/checkout", {"tier": "standard", "code": "ROOK-FRIEND"}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["reserved"] == [(user.id, "ROOK-FRIEND", "referral")]
    assert captured["attached"] == [(captured["reservation_id"], "cs_live_9")]
    assert captured["released"] == []


@pytest.mark.asyncio
async def test_second_concurrent_checkout_is_refused_before_stripe(
    stripe_configured, monkeypatch
):
    """The same code in two tabs. The second reservation loses at the database,
    and no discounted Stripe session is created for it — without this, both
    sessions are payable and the discount is applied twice."""
    from backend.services.billing import stripe_gateway

    _fake_resolve(monkeypatch, _resolved(), reservation_id=None)
    calls = []
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: calls.append(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/checkout", {"tier": "standard", "code": "ROOK-FRIEND"}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert "already open" in resp.json()["detail"]
    assert calls == []


@pytest.mark.asyncio
async def test_checkout_releases_the_reservation_when_stripe_fails(
    stripe_configured, monkeypatch
):
    """No session was created, so the slot we are holding belongs to a checkout
    that does not exist. Holding it would lock the user out of their discount for
    the whole pending TTL."""
    from backend.services.billing import stripe_gateway

    captured = _fake_resolve(monkeypatch, _resolved())

    def _boom(**kw):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(stripe_gateway, "create_checkout_session", _boom)

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        with pytest.raises(RuntimeError):
            await _post(
                "/api/billing/checkout", {"tier": "standard", "code": "ROOK-FRIEND"}
            )
    finally:
        app.dependency_overrides.clear()

    assert captured["released"] == [captured["reservation_id"]]
    assert captured["attached"] == []


@pytest.mark.asyncio
async def test_checkout_without_a_code_reserves_nothing(stripe_configured, monkeypatch):
    """No discount, no slot to take."""
    from backend.services.billing import stripe_gateway

    captured = _fake_resolve(monkeypatch, _resolved())
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session", lambda **kw: _session()
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/checkout", {"tier": "standard"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["reserved"] == []


@pytest.mark.asyncio
async def test_checkout_with_invalid_code_is_400_and_never_reaches_stripe(
    stripe_configured, monkeypatch
):
    from backend.services.billing import stripe_gateway

    _fake_resolve(
        monkeypatch,
        _resolved(
            valid=False, kind=None, percent_off=0, referrer_user_id=None,
            message="You cannot use your own referral code.",
        ),
    )
    calls = []
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: calls.append(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/checkout", {"tier": "standard", "code": "ROOK-MINE01"}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert resp.json()["detail"] == "You cannot use your own referral code."
    assert calls == []


@pytest.mark.asyncio
async def test_checkout_without_code_sends_no_discounts(stripe_configured, monkeypatch):
    """Stripe rejects an empty discounts list, so the parameter must be absent."""
    from backend.services.billing import stripe_gateway

    captured = {}
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: captured.update(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/checkout", {"tier": "standard"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert captured["discounts"] is None
    assert "redeemed_code" not in captured["metadata"]


@pytest.mark.asyncio
async def test_checkout_pack_with_code_is_rejected(stripe_configured, monkeypatch):
    """Ignoring the code would charge full price while the user believes a
    discount was applied."""
    from backend.services.billing import stripe_gateway

    calls = []
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: calls.append(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/checkout", {"pack": "credits_100", "code": "ROOK-FRIEND"}
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert calls == []


@pytest.mark.asyncio
async def test_checkout_season_with_code_is_rejected(stripe_configured, monkeypatch):
    """Season passes are one-time payments — no first month to discount, and no
    recurring invoice for the referrer's reward to attach to."""
    from backend.services.billing import stripe_gateway

    calls = []
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: calls.append(kw) or _session(),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/checkout",
            {"tier": "standard", "interval": "season", "code": "ROOK-FRIEND"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400
    assert "monthly" in resp.json()["detail"].lower()
    assert calls == []


# ── validate-code ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_code_reports_the_discount_without_applying_it(
    stripe_configured, monkeypatch
):
    from backend.services.billing import stripe_gateway

    captured_args = _fake_resolve(monkeypatch, _resolved())
    monkeypatch.setattr(
        stripe_gateway, "create_checkout_session",
        lambda **kw: (_ for _ in ()).throw(AssertionError("must not create a session")),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post(
            "/api/billing/validate-code",
            {"code": "ROOK-FRIEND", "interval": "monthly"},
        )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["percent_off"] == 30
    # Bound to the authenticated user, never a client-supplied id.
    assert captured_args["redeemer_user_id"] == user.id


@pytest.mark.asyncio
async def test_validate_code_returns_the_rejection_message(stripe_configured, monkeypatch):
    _fake_resolve(
        monkeypatch,
        _resolved(
            valid=False, kind=None, percent_off=0, referrer_user_id=None,
            message="That code is not valid.",
        ),
    )

    user = _make_user(customer_id="cus_1")
    _override_auth(user)
    try:
        resp = await _post("/api/billing/validate-code", {"code": "ROOK-NOPE01"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["percent_off"] == 0
    assert body["message"] == "That code is not valid."


# ── pricing sheet ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pricing_serves_referral_percentages_from_the_source_of_truth():
    from backend.models.user import REFERRAL_PROGRAM

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/api/billing/pricing")

    assert resp.status_code == 200
    referral = resp.json()["referral"]
    assert referral["welcome_percent_off"] == REFERRAL_PROGRAM["welcome_percent_off"]
    assert referral["referred_percent_off"] == REFERRAL_PROGRAM["referred_percent_off"]
    assert (
        referral["referrer_percent_off_per_referral"]
        == REFERRAL_PROGRAM["referrer_percent_off_per_referral"]
    )
    assert (
        referral["referrer_percent_off_cap"]
        == REFERRAL_PROGRAM["referrer_percent_off_cap"]
    )
    assert referral["eligible_intervals"] == list(
        REFERRAL_PROGRAM["eligible_intervals"]
    )
