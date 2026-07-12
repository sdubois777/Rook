"""
Tests for StripeWebhookService — the event state machine under the tier/credit
spec: NO signup bonuses on purchase (free's 30 is granted at account creation),
NO monthly credit grants (deleted), SEASON one-time entitlements with expiry,
single credit pack, downgrade target = free.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.user_service import UserService
from backend.services.billing.webhook_service import StripeWebhookService


# ── fakes ───────────────────────────────────────────────────────────────

class FakeEventRepo:
    def __init__(self):
        self.seen = set()

    async def mark_processed(self, event_id):
        if event_id in self.seen:
            return False
        self.seen.add(event_id)
        return True


class FakePackRepo:
    def __init__(self):
        self.granted = {}

    async def record_grant(self, session_id, user_id, credits):
        if session_id in self.granted:
            return False
        self.granted[session_id] = (user_id, credits)
        return True


class FakeLeagueReconciler:
    def __init__(self):
        self.calls = []

    async def reconcile_for_tier(self, user_id, tier):
        self.calls.append((user_id, tier))


class FakeUserRepo:
    """Duck-types the UserRepository methods the webhook + UserService touch."""

    def __init__(self, user):
        self._user = user

    async def get_by_stripe_customer_id(self, customer_id):
        if self._user.stripe_customer_id == customer_id:
            return self._user
        return None

    async def get_or_404(self, user_id):
        return self._user

    async def update_tier(self, user_id, tier, credits_bonus=0):
        self._user.tier = tier
        self._user.credits_remaining += credits_bonus
        return self._user

    async def update_credits(self, user_id, delta):
        self._user.credits_remaining += delta
        return self._user.credits_remaining

    async def set_stripe_subscription_id(self, user_id, subscription_id):
        self._user.stripe_subscription_id = subscription_id

    async def set_subscription_status(self, user_id, status):
        self._user.subscription_status = status

    async def set_tier_expiry(self, user_id, expires_at):
        self._user.tier_expires_at = expires_at

    async def commit(self):  # should not be hit (commit=False everywhere)
        raise AssertionError("service must not commit via the user repo")


def _make_user(tier="free", credits=30, customer_id="cus_1"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        external_id="user_abc",
        email="u@example.com",
        tier=tier,
        tier_expires_at=None,
        credits_remaining=credits,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        subscription_status=None,
    )


def _build(user):
    repo = FakeUserRepo(user)
    db = MagicMock()
    db.commit = AsyncMock()
    service = StripeWebhookService(
        db,
        user_repo=repo,
        user_service=UserService(repo),
        events=FakeEventRepo(),
        packs=(packs := FakePackRepo()),
        leagues=(leagues := FakeLeagueReconciler()),
    )
    service._test_packs = packs
    service._test_leagues = leagues
    return service, db


def _event(event_type, obj, event_id="evt_1"):
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def _sub_obj(customer="cus_1", price="price_standard", status="active",
             cancel=False, sub_id="sub_1"):
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel,
        "items": {"data": [{"price": {"id": price}}]},
    }


@pytest.fixture(autouse=True)
def _price_env(monkeypatch):
    """Point the catalog at deterministic price ids for these tests."""
    from backend.services.billing import catalog
    monkeypatch.setattr(catalog.settings, "stripe_price_standard_monthly", "price_standard", raising=False)
    monkeypatch.setattr(catalog.settings, "stripe_price_standard_season", "price_standard_s", raising=False)
    monkeypatch.setattr(catalog.settings, "stripe_price_pro_monthly", "price_pro", raising=False)
    monkeypatch.setattr(catalog.settings, "stripe_price_pro_season", "price_pro_s", raising=False)
    monkeypatch.setattr(catalog.settings, "stripe_price_pack_100", "price_pack", raising=False)


# ── checkout.session.completed — monthly subscription ───────────────────

@pytest.mark.asyncio
async def test_checkout_subscription_upgrades_no_bonus_and_clears_expiry():
    user = _make_user(tier="free", credits=30)
    user.tier_expires_at = datetime.now(timezone.utc) + timedelta(days=100)  # old season
    service, db = _build(user)

    obj = {
        "customer": "cus_1",
        "mode": "subscription",
        "subscription": "sub_1",
        "metadata": {"tier": "standard", "interval": "monthly"},
    }
    result = await service.process(_event("checkout.session.completed", obj))

    assert result.handled
    assert user.tier == "standard"
    assert user.credits_remaining == 30      # NO purchase bonus under the new spec
    assert user.tier_expires_at is None      # monthly supersedes season expiry
    assert user.stripe_subscription_id == "sub_1"
    assert user.subscription_status == "active"
    db.commit.assert_awaited_once()


# ── checkout.session.completed — SEASON one-time purchase ───────────────

@pytest.mark.asyncio
async def test_checkout_season_sets_tier_and_expiry():
    user = _make_user(tier="free", credits=30)
    service, _db = _build(user)

    obj = {
        "id": "cs_season_1",
        "customer": "cus_1",
        "mode": "payment",
        "metadata": {"tier": "pro", "interval": "season"},
    }
    await service.process(_event("checkout.session.completed", obj))

    assert user.tier == "pro"
    assert user.credits_remaining == 30                  # no bonus
    assert user.tier_expires_at is not None              # season entitlement end
    assert user.tier_expires_at > datetime.now(timezone.utc)
    assert user.tier_expires_at.month == 3 and user.tier_expires_at.day == 1
    assert (user.id, "pro") in service._test_leagues.calls


@pytest.mark.asyncio
async def test_checkout_season_cancels_active_monthly_at_period_end(monkeypatch):
    """A season purchase supersedes an active monthly sub — best-effort
    cancel_at_period_end so the user isn't double-billed."""
    from backend.services.billing import stripe_gateway

    called = {}
    monkeypatch.setattr(
        stripe_gateway, "cancel_at_period_end",
        lambda *, sub_id, idempotency_key: called.setdefault("sub", sub_id),
    )
    user = _make_user(tier="standard", credits=0)
    user.stripe_subscription_id = "sub_live"
    service, _db = _build(user)

    obj = {
        "id": "cs_season_2",
        "customer": "cus_1",
        "mode": "payment",
        "metadata": {"tier": "pro", "interval": "season"},
    }
    await service.process(_event("checkout.session.completed", obj))
    assert called["sub"] == "sub_live"
    assert user.tier == "pro"


# ── checkout.session.completed — credit pack ────────────────────────────

@pytest.mark.asyncio
async def test_checkout_payment_grants_pack_credits_no_tier_change():
    user = _make_user(tier="free", credits=30)
    service, _db = _build(user)

    obj = {
        "id": "cs_pack_1",
        "customer": "cus_1",
        "mode": "payment",
        "metadata": {"pack": "credits_100", "credits": "100"},
    }
    await service.process(_event("checkout.session.completed", obj))

    assert user.tier == "free"
    assert user.credits_remaining == 30 + 100
    assert "cs_pack_1" in service._test_packs.granted


@pytest.mark.asyncio
async def test_pack_grant_idempotent_on_session_id():
    """A redelivered pack completion under a DIFFERENT event id grants once."""
    user = _make_user(tier="free", credits=30)
    service, _db = _build(user)

    obj = {
        "id": "cs_pack_9",
        "customer": "cus_1",
        "mode": "payment",
        "metadata": {"pack": "credits_100", "credits": "100"},
    }
    await service.process(_event("checkout.session.completed", obj, event_id="evt_a"))
    await service.process(_event("checkout.session.completed", obj, event_id="evt_b"))

    assert user.credits_remaining == 30 + 100  # granted exactly once


# ── customer.subscription.created / updated ─────────────────────────────

@pytest.mark.asyncio
async def test_subscription_created_reconciles_tier_without_bonus():
    user = _make_user(tier="free", credits=30)
    service, _db = _build(user)

    await service.process(
        _event("customer.subscription.created", _sub_obj(price="price_pro"))
    )
    assert user.tier == "pro"
    assert user.credits_remaining == 30  # reconcile only — never a bonus


@pytest.mark.asyncio
async def test_subscription_updated_cancel_scheduled_does_not_downgrade():
    user = _make_user(tier="pro", credits=200)
    service, _db = _build(user)

    await service.process(
        _event("customer.subscription.updated",
               _sub_obj(price="price_pro", status="active", cancel=True))
    )
    assert user.tier == "pro"  # keeps tier through paid period
    assert user.subscription_status == "canceling"


@pytest.mark.asyncio
async def test_subscription_updated_past_due_marks_status_no_downgrade():
    user = _make_user(tier="pro", credits=200)
    service, _db = _build(user)

    await service.process(
        _event("customer.subscription.updated",
               _sub_obj(price="price_pro", status="past_due"))
    )
    assert user.tier == "pro"
    assert user.subscription_status == "past_due"


# ── customer.subscription.deleted ───────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_deleted_downgrades_to_free_credits_persist():
    user = _make_user(tier="pro", credits=200)
    user.stripe_subscription_id = "sub_1"
    service, _db = _build(user)

    await service.process(
        _event("customer.subscription.deleted", _sub_obj(status="canceled"))
    )
    assert user.tier == "free"
    assert user.credits_remaining == 200  # credits SURVIVE the downgrade
    assert user.stripe_subscription_id is None
    assert (user.id, "free") in service._test_leagues.calls


@pytest.mark.asyncio
async def test_subscription_deleted_keeps_unexpired_season_entitlement():
    """Monthly sub ends after a season purchase superseded it — the season
    entitlement holds; no downgrade."""
    user = _make_user(tier="pro", credits=0)
    user.stripe_subscription_id = "sub_1"
    user.tier_expires_at = datetime.now(timezone.utc) + timedelta(days=90)
    service, _db = _build(user)

    await service.process(
        _event("customer.subscription.deleted", _sub_obj(status="canceled"))
    )
    assert user.tier == "pro"                 # season entitlement holds
    assert user.stripe_subscription_id is None


# ── invoice events: monthly credit grants are DELETED ───────────────────

@pytest.mark.asyncio
async def test_invoice_payment_succeeded_is_unhandled_no_grant():
    """The monthly-credit-grant machinery is deleted — a cycle invoice grants
    NOTHING (paid tiers are unlimited; credits are the free tier's meter)."""
    user = _make_user(tier="standard", credits=100)
    service, _db = _build(user)

    obj = {"id": "in_1", "customer": "cus_1", "billing_reason": "subscription_cycle"}
    result = await service.process(_event("invoice.payment_succeeded", obj))

    assert not result.handled                 # no handler registered anymore
    assert user.credits_remaining == 100      # nothing granted


@pytest.mark.asyncio
async def test_invoice_payment_failed_marks_past_due():
    user = _make_user(tier="standard", credits=0)
    service, _db = _build(user)
    obj = {"id": "in_2", "customer": "cus_1"}
    await service.process(_event("invoice.payment_failed", obj))
    assert user.subscription_status == "past_due"


# ── idempotency layer 1 (event.id) + unknown customer ───────────────────

@pytest.mark.asyncio
async def test_redelivered_event_id_is_a_noop():
    user = _make_user(tier="free", credits=30)
    service, _db = _build(user)

    obj = {
        "id": "cs_pack_2", "customer": "cus_1", "mode": "payment",
        "metadata": {"pack": "credits_100", "credits": "100"},
    }
    ev = _event("checkout.session.completed", obj, event_id="evt_same")
    r1 = await service.process(ev)
    r2 = await service.process(ev)

    assert r1.handled and not r1.duplicate
    assert r2.duplicate
    assert user.credits_remaining == 30 + 100  # granted exactly once


@pytest.mark.asyncio
async def test_unknown_customer_is_a_safe_noop():
    user = _make_user(tier="standard", credits=100, customer_id="cus_1")
    service, _db = _build(user)

    obj = {"id": "cs_x", "customer": "cus_OTHER", "mode": "payment",
           "metadata": {"pack": "credits_100", "credits": "100"}}
    result = await service.process(_event("checkout.session.completed", obj))

    assert result.handled
    assert user.credits_remaining == 100  # untouched
