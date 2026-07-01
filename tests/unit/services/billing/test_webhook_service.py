"""
Tests for StripeWebhookService — the §4 event state machine.

Uses fakes for the DB-backed collaborators (idempotency repos + user repo) so the
dispatch/entitlement logic is exercised without a real database, per the unit-suite
rule. The REAL UserService drives tier/credit writes against a fake user repo, so
the grant_signup_bonus / commit=False behavior is covered end to end.
"""
from __future__ import annotations

import uuid
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


class FakeInvoiceRepo:
    def __init__(self):
        self.granted = {}

    async def record_grant(self, invoice_id, user_id, credits):
        if invoice_id in self.granted:
            return False
        self.granted[invoice_id] = (user_id, credits)
        return True


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

    async def commit(self):  # should not be hit (commit=False everywhere)
        raise AssertionError("service must not commit via the user repo")


def _make_user(tier="intro", credits=25, customer_id="cus_1"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        external_id="user_abc",
        email="u@example.com",
        tier=tier,
        credits_remaining=credits,
        stripe_customer_id=customer_id,
        stripe_subscription_id=None,
        subscription_status=None,
    )


def _build(user):
    repo = FakeUserRepo(user)
    db = MagicMock()
    db.commit = AsyncMock()
    events = FakeEventRepo()
    invoices = FakeInvoiceRepo()
    service = StripeWebhookService(
        db,
        user_repo=repo,
        user_service=UserService(repo),
        events=events,
        invoices=invoices,
    )
    return service, db, events, invoices


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
    monkeypatch.setattr(catalog.settings, "stripe_price_intro_monthly", "price_intro", raising=False)
    monkeypatch.setattr(catalog.settings, "stripe_price_standard_monthly", "price_standard", raising=False)
    monkeypatch.setattr(catalog.settings, "stripe_price_pro_monthly", "price_pro", raising=False)


# ── checkout.session.completed ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_checkout_subscription_upgrades_and_grants_bonus_once():
    user = _make_user(tier="intro", credits=25)
    service, db, *_ = _build(user)

    obj = {
        "customer": "cus_1",
        "mode": "subscription",
        "subscription": "sub_1",
        "metadata": {"tier": "standard"},
    }
    result = await service.process(_event("checkout.session.completed", obj))

    assert result.handled
    assert user.tier == "standard"
    assert user.credits_remaining == 25 + 75  # standard signup bonus, once
    assert user.stripe_subscription_id == "sub_1"
    assert user.subscription_status == "active"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_checkout_payment_grants_pack_credits_no_tier_change():
    user = _make_user(tier="intro", credits=25)
    service, *_ = _build(user)

    obj = {
        "customer": "cus_1",
        "mode": "payment",
        "metadata": {"pack": "medium", "credits": "175"},
    }
    await service.process(_event("checkout.session.completed", obj))

    assert user.tier == "intro"
    assert user.credits_remaining == 25 + 175


# ── customer.subscription.created ───────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_created_noops_when_tier_already_set():
    user = _make_user(tier="standard", credits=100)
    service, *_ = _build(user)

    await service.process(
        _event("customer.subscription.created", _sub_obj(price="price_standard"))
    )
    assert user.tier == "standard"
    assert user.credits_remaining == 100  # no second signup bonus
    assert user.stripe_subscription_id == "sub_1"
    assert user.subscription_status == "active"


@pytest.mark.asyncio
async def test_subscription_created_reconciles_tier_without_bonus():
    user = _make_user(tier="intro", credits=25)
    service, *_ = _build(user)

    await service.process(
        _event("customer.subscription.created", _sub_obj(price="price_pro"))
    )
    assert user.tier == "pro"
    assert user.credits_remaining == 25  # reconcile only — NO signup bonus


# ── customer.subscription.updated ───────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_updated_tier_change_no_bonus():
    user = _make_user(tier="standard", credits=100)
    user.stripe_subscription_id = "sub_1"
    service, *_ = _build(user)

    await service.process(
        _event("customer.subscription.updated",
                _sub_obj(price="price_pro", status="active"))
    )
    assert user.tier == "pro"
    assert user.credits_remaining == 100  # plan swap grants no signup bonus
    assert user.subscription_status == "active"


@pytest.mark.asyncio
async def test_subscription_updated_cancel_scheduled_does_not_downgrade():
    user = _make_user(tier="pro", credits=200)
    service, *_ = _build(user)

    await service.process(
        _event("customer.subscription.updated",
                _sub_obj(price="price_pro", status="active", cancel=True))
    )
    assert user.tier == "pro"  # keeps tier through paid period
    assert user.subscription_status == "canceling"


@pytest.mark.asyncio
async def test_subscription_updated_past_due_marks_status_no_downgrade():
    user = _make_user(tier="pro", credits=200)
    service, *_ = _build(user)

    await service.process(
        _event("customer.subscription.updated",
                _sub_obj(price="price_pro", status="past_due"))
    )
    assert user.tier == "pro"
    assert user.subscription_status == "past_due"


# ── customer.subscription.deleted ───────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_deleted_is_the_only_downgrade_credits_persist():
    user = _make_user(tier="pro", credits=200)
    user.stripe_subscription_id = "sub_1"
    service, *_ = _build(user)

    await service.process(
        _event("customer.subscription.deleted", _sub_obj(status="canceled"))
    )
    assert user.tier == "intro"
    assert user.credits_remaining == 200  # credits SURVIVE the downgrade
    assert user.stripe_subscription_id is None
    assert user.subscription_status == "active"


# ── invoice.payment_succeeded (grant filter + idempotency layer 2) ──────

@pytest.mark.asyncio
async def test_invoice_cycle_grants_monthly_credits():
    user = _make_user(tier="standard", credits=100)
    service, *_ = _build(user)

    obj = {
        "id": "in_1",
        "customer": "cus_1",
        "billing_reason": "subscription_cycle",
    }
    await service.process(_event("invoice.payment_succeeded", obj))
    assert user.credits_remaining == 100 + 20  # standard monthly


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["subscription_create", "subscription_update"])
async def test_invoice_non_cycle_does_not_grant(reason):
    user = _make_user(tier="standard", credits=100)
    service, _db, _ev, invoices = _build(user)

    obj = {"id": "in_2", "customer": "cus_1", "billing_reason": reason}
    await service.process(_event("invoice.payment_succeeded", obj))

    assert user.credits_remaining == 100  # no top-up
    assert "in_2" not in invoices.granted


@pytest.mark.asyncio
async def test_invoice_grant_idempotent_on_invoice_id():
    """Two DISTINCT events referencing the same invoice grant credits once."""
    user = _make_user(tier="pro", credits=200)
    service, *_ = _build(user)

    obj = {"id": "in_9", "customer": "cus_1", "billing_reason": "subscription_cycle"}
    await service.process(_event("invoice.payment_succeeded", obj, event_id="evt_a"))
    await service.process(_event("invoice.payment_succeeded", obj, event_id="evt_b"))

    assert user.credits_remaining == 200 + 50  # pro monthly, exactly once


# ── idempotency layer 1 (event.id) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_redelivered_event_id_is_a_noop():
    user = _make_user(tier="standard", credits=100)
    service, db, *_ = _build(user)

    obj = {"id": "in_5", "customer": "cus_1", "billing_reason": "subscription_cycle"}
    ev = _event("invoice.payment_succeeded", obj, event_id="evt_same")

    r1 = await service.process(ev)
    r2 = await service.process(ev)  # exact redelivery

    assert r1.handled and not r1.duplicate
    assert r2.duplicate
    assert user.credits_remaining == 100 + 20  # granted exactly once


# ── unknown customer ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_customer_is_a_safe_noop():
    user = _make_user(tier="standard", credits=100, customer_id="cus_1")
    service, *_ = _build(user)

    obj = {"id": "in_7", "customer": "cus_OTHER", "billing_reason": "subscription_cycle"}
    result = await service.process(_event("invoice.payment_succeeded", obj))

    assert result.handled  # dispatched, but resolved no user
    assert user.credits_remaining == 100  # untouched
