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
    """Models layer-1 dedup AS A TRANSACTION: mark_processed stages a pending
    insert; db.commit persists it, db.rollback discards it — so a failed event is
    not durably recorded and a redelivery reprocesses (wired in _build)."""

    def __init__(self):
        self.committed = set()
        self._pending = set()

    async def mark_processed(self, event_id):
        if event_id in self.committed or event_id in self._pending:
            return False
        self._pending.add(event_id)
        return True

    def _commit(self):
        self.committed |= self._pending
        self._pending = set()

    def _rollback(self):
        self._pending = set()


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


class FakeReferralRepo:
    """In-memory stand-in for ReferralRepository.

    Models the two things the webhook depends on: a reservation flips to
    confirmed exactly once, and record_redemption is insert-or-skip on the
    session id.
    """

    def __init__(
        self, *, pending=(), confirmed_count=0, redemption=None, counts_by_user=None
    ):
        # session ids that were reserved at checkout time and not yet confirmed
        self._pending = set(pending)
        self._confirmed = set()
        self._confirmed_count = confirmed_count
        # Per-user override. The count is asked for TWO different users during one
        # checkout — the referrer being rewarded, and the subscribing customer
        # themselves, whose own earned rate is pushed onto the subscription they
        # just started. Answering the same number for both makes every new
        # subscriber look like they already had referrals, which is not a state
        # that can exist. Tests that care set this per user id.
        self._counts_by_user = dict(counts_by_user or {})
        self._redemption = redemption
        self.recorded = []

    async def confirm_redemption(self, stripe_session_id):
        if stripe_session_id not in self._pending:
            return False
        self._pending.discard(stripe_session_id)
        self._confirmed.add(stripe_session_id)
        return True

    async def record_redemption(self, **kwargs):
        session_id = kwargs["stripe_session_id"]
        if session_id in self._confirmed:
            return False
        self._confirmed.add(session_id)
        self.recorded.append(kwargs)
        return True

    async def confirmed_referral_count(self, referrer_user_id):
        return self._counts_by_user.get(referrer_user_id, self._confirmed_count)

    async def redemption_for_redeemer(self, redeemer_user_id):
        return self._redemption


class FakeUserRepo:
    """Duck-types the UserRepository methods the webhook + UserService touch."""

    def __init__(self, user):
        self._user = user
        # Extra rows the webhook can look up by id (a referrer, for instance).
        self.by_id = {user.id: user}

    async def get_by_stripe_customer_id(self, customer_id):
        if self._user.stripe_customer_id == customer_id:
            return self._user
        return None

    async def get(self, user_id):
        return self.by_id.get(user_id)

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


def _build(user, referrals=None):
    repo = FakeUserRepo(user)
    events = FakeEventRepo()
    db = MagicMock()
    # Wire commit/rollback to the fake event repo's txn state so "recorded only on
    # success" and "rolled back on failure" are observable.
    db.commit = AsyncMock(side_effect=events._commit)
    db.rollback = AsyncMock(side_effect=events._rollback)
    service = StripeWebhookService(
        db,
        user_repo=repo,
        user_service=UserService(repo),
        events=events,
        packs=(packs := FakePackRepo()),
        leagues=(leagues := FakeLeagueReconciler()),
        referrals=(referrals or FakeReferralRepo()),
    )
    service._test_packs = packs
    service._test_leagues = leagues
    service._test_users = repo
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
async def test_subscription_created_clears_stale_season_expiry():
    """A monthly sub supersedes any season expiry — even when the sub arrives
    WITHOUT the checkout handler (dashboard-created, or checkout.session.completed
    never landing). A stale past-dated tier_expires_at from a prior season pass
    must be cleared, or effective_tier() reads the paying subscriber as free and
    the /account/me lazy write-back persists the downgrade."""
    from backend.models.user import effective_tier

    user = _make_user(tier="free", credits=30)
    user.tier_expires_at = datetime.now(timezone.utc) - timedelta(days=30)
    service, _db = _build(user)

    await service.process(
        _event("customer.subscription.created", _sub_obj(price="price_pro"))
    )
    assert user.tier_expires_at is None
    assert effective_tier(user) == "pro"  # not "free" — the stale expiry is gone


@pytest.mark.asyncio
async def test_subscription_created_unmapped_price_leaves_expiry_untouched():
    """No paid tier granted (price doesn't map) → the expiry is NOT cleared."""
    stale = datetime.now(timezone.utc) - timedelta(days=30)
    user = _make_user(tier="free", credits=30)
    user.tier_expires_at = stale
    service, _db = _build(user)

    await service.process(
        _event("customer.subscription.created", _sub_obj(price="price_mystery"))
    )
    assert user.tier == "free"              # nothing granted
    assert user.tier_expires_at == stale    # expiry untouched


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


# ── referral redemption + reward ────────────────────────────────────────

def _plain_subscription_checkout(session_id="cs_plain_1"):
    """A completed subscription checkout carrying NO discount code — the ordinary
    case, and the one where the subscriber's own earned referrer rate applies."""
    return {
        "id": session_id,
        "customer": "cus_1",
        "mode": "subscription",
        "subscription": "sub_new",
        "metadata": {"tier": "standard", "interval": "monthly"},
    }


def _referral_checkout(referrer_id, session_id="cs_ref_1", percent_off=30):
    """A completed subscription checkout carrying the metadata our own checkout
    endpoint wrote when it applied a referral code."""
    return {
        "id": session_id,
        "customer": "cus_1",
        "mode": "subscription",
        "subscription": "sub_new",
        "metadata": {
            "tier": "standard",
            "interval": "monthly",
            "redeemed_code": "ROOK-FRIEND",
            "redeemed_kind": "referral",
            "redeemed_percent_off": str(percent_off),
            "referrer_user_id": str(referrer_id) if referrer_id else "",
        },
    }


@pytest.fixture
def captured_discounts(monkeypatch):
    """Record every set_subscription_discount call instead of reaching Stripe."""
    from backend.services.billing import stripe_gateway

    calls = []
    monkeypatch.setattr(
        stripe_gateway, "set_subscription_discount",
        lambda **kw: calls.append(kw),
    )
    return calls


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    """Referral emails are covered separately; keep them out of every other test."""
    from backend.services.email.email_service import EmailService

    monkeypatch.setattr(
        EmailService, "send_referral_reward", AsyncMock(return_value="skipped")
    )


def _referrer(user_repo, *, subscription_id="sub_referrer"):
    referrer = _make_user(tier="standard", credits=0, customer_id="cus_referrer")
    referrer.stripe_subscription_id = subscription_id
    user_repo.by_id[referrer.id] = referrer
    return referrer


@pytest.mark.asyncio
async def test_checkout_confirms_the_reservation_and_raises_the_referrer_rate(
    captured_discounts,
):
    """The whole money path: the reservation written at checkout time is
    confirmed, and the referrer's coupon is recomputed from the live count."""
    from backend.models.user import referrer_percent_off

    user = _make_user(tier="free", credits=30)
    referrals = FakeReferralRepo(pending={"cs_ref_1"}, confirmed_count=2)
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users)

    obj = _referral_checkout(referrer.id)
    await service.process(_event("checkout.session.completed", obj))

    assert user.tier == "standard"
    assert captured_discounts[0]["sub_id"] == "sub_referrer"
    # Two live referrals => the summed rate, from REFERRAL_PROGRAM.
    assert captured_discounts[0]["coupon_id"].endswith(str(referrer_percent_off(2)))


@pytest.mark.asyncio
async def test_referral_reward_moves_exactly_once_under_redelivery(
    captured_discounts,
):
    """Stripe delivers at least once and a redelivery may carry a different event
    id, so the session id is the only stable key."""
    user = _make_user(tier="free", credits=30)
    # The subscribing customer has referred nobody, so the only coupon pushed is
    # the referrer's reward. Without this the fake answers 1 for every user id,
    # including the brand-new subscriber, and their own earned rate is pushed too.
    referrals = FakeReferralRepo(
        pending={"cs_ref_1"}, confirmed_count=1, counts_by_user={user.id: 0}
    )
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users)

    obj = _referral_checkout(referrer.id)
    await service.process(_event("checkout.session.completed", obj, event_id="evt_a"))
    await service.process(_event("checkout.session.completed", obj, event_id="evt_b"))

    assert len(captured_discounts) == 1


@pytest.mark.asyncio
async def test_checkout_records_the_redemption_when_no_reservation_exists(
    captured_discounts,
):
    """The reservation is the normal path, not the only one. A completed checkout
    with no pending row is still recorded — once."""
    user = _make_user(tier="free", credits=30)
    # Nothing pending, and the subscribing customer has referred nobody — so the
    # single coupon pushed here is the referrer's reward, not their own rate.
    referrals = FakeReferralRepo(confirmed_count=1, counts_by_user={user.id: 0})
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users)

    obj = _referral_checkout(referrer.id)
    await service.process(_event("checkout.session.completed", obj, event_id="evt_a"))
    await service.process(_event("checkout.session.completed", obj, event_id="evt_b"))

    assert len(referrals.recorded) == 1
    assert referrals.recorded[0]["stripe_session_id"] == "cs_ref_1"
    assert referrals.recorded[0]["redeemer_user_id"] == user.id
    assert referrals.recorded[0]["referrer_user_id"] == referrer.id
    assert len(captured_discounts) == 1


@pytest.mark.asyncio
async def test_welcome_redemption_pays_nobody(captured_discounts):
    """A welcome code has no referrer — the metadata field is present and empty,
    because Stripe metadata cannot hold a null."""
    user = _make_user(tier="free", credits=30)
    referrals = FakeReferralRepo(pending={"cs_ref_1"})
    service, _db = _build(user, referrals)

    obj = _referral_checkout(None)
    obj["metadata"]["redeemed_kind"] = "welcome"
    await service.process(_event("checkout.session.completed", obj))

    assert captured_discounts == []


@pytest.mark.asyncio
async def test_referrer_without_a_subscription_is_skipped_cleanly(captured_discounts):
    """A referrer on the free tier has no recurring invoice to discount. Their
    rate is applied later, by _apply_own_referrer_rate, on the subscription they
    start themselves — see test_a_referrer_who_subscribes_later_gets_their_rate."""
    user = _make_user(tier="free", credits=30)
    referrals = FakeReferralRepo(
        pending={"cs_ref_1"}, confirmed_count=1, counts_by_user={user.id: 0}
    )
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users, subscription_id=None)

    result = await service.process(
        _event("checkout.session.completed", _referral_checkout(referrer.id))
    )

    assert result.handled
    assert captured_discounts == []


@pytest.mark.asyncio
async def test_a_failing_stripe_coupon_call_does_not_fail_the_webhook(monkeypatch):
    """The entitlement is already granted. Failing here would roll it back and
    make Stripe redeliver a payment we already applied."""
    from backend.services.billing import stripe_gateway

    def _boom(**kw):
        raise RuntimeError("stripe is down")

    monkeypatch.setattr(stripe_gateway, "set_subscription_discount", _boom)

    user = _make_user(tier="free", credits=30)
    referrals = FakeReferralRepo(pending={"cs_ref_1"}, confirmed_count=1)
    service, db = _build(user, referrals)
    referrer = _referrer(service._test_users)

    result = await service.process(
        _event("checkout.session.completed", _referral_checkout(referrer.id))
    )

    assert result.handled
    assert user.tier == "standard"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_referrer_who_subscribes_later_gets_their_earned_rate(
    captured_discounts,
):
    """Referrals earned while on the free tier are not lost.

    A referrer's coupon is otherwise only pushed when a new referral lands or
    when one goes away, and both need a live subscription to write to. Every
    reward earned before the referrer subscribed used to be skipped and never
    revisited, so someone who referred five friends and then subscribed started
    at 0 percent.
    """
    user = _make_user(tier="free", credits=30)
    # This customer referred three people while they were on the free tier. Their
    # own checkout carries no discount code, so the reward push is the only one.
    referrals = FakeReferralRepo(counts_by_user={user.id: 3})
    service, _db = _build(user, referrals)

    from backend.models.user import referrer_percent_off
    from backend.services.billing.catalog import referrer_coupon_id

    await service.process(
        _event("checkout.session.completed", _plain_subscription_checkout())
    )

    assert len(captured_discounts) == 1
    applied = captured_discounts[0]
    assert applied["coupon_id"] == referrer_coupon_id(referrer_percent_off(3))
    # Written to the subscription this checkout just started.
    assert applied["sub_id"] == "sub_new"
    # The "self_" marker keeps this push from ever sharing an idempotency key
    # with the reward push for the REFERRER of the same checkout session.
    assert "self_" in applied["idempotency_key"]


@pytest.mark.asyncio
async def test_a_subscriber_with_no_referrals_has_no_discount_touched(
    captured_discounts,
):
    """Pushing a 0 percent rate would CLEAR the subscription's discount list,
    including the one-time coupon from a code the customer just redeemed. A user
    with no referrals must have their discounts left alone entirely."""
    user = _make_user(tier="free", credits=30)
    referrals = FakeReferralRepo(counts_by_user={user.id: 0})
    service, _db = _build(user, referrals)

    await service.process(
        _event("checkout.session.completed", _plain_subscription_checkout())
    )

    assert captured_discounts == []


@pytest.mark.asyncio
async def test_a_raising_referral_email_does_not_fail_the_webhook(
    monkeypatch, captured_discounts
):
    from backend.services.email.email_service import EmailService

    async def _boom(self, **kwargs):
        raise RuntimeError("the mail provider is down")

    monkeypatch.setattr(EmailService, "send_referral_reward", _boom)

    user = _make_user(tier="free", credits=30)
    # Subscribing customer has referred nobody, so exactly one coupon is pushed.
    referrals = FakeReferralRepo(
        pending={"cs_ref_1"}, confirmed_count=1, counts_by_user={user.id: 0}
    )
    service, db = _build(user, referrals)
    referrer = _referrer(service._test_users)

    result = await service.process(
        _event("checkout.session.completed", _referral_checkout(referrer.id))
    )

    assert result.handled
    assert user.tier == "standard"
    assert len(captured_discounts) == 1   # the coupon still went out
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_referral_email_tells_the_referrer_the_new_rate(monkeypatch):
    from backend.models.user import referrer_percent_off
    from backend.services.email.email_service import EmailService

    sent = {}

    async def _capture(self, *, user, new_total_percent, referral_count):
        sent.update(
            to=user.id,
            percent=new_total_percent,
            count=referral_count,
        )
        return "sent"

    monkeypatch.setattr(EmailService, "send_referral_reward", _capture)
    monkeypatch.setattr(
        "backend.services.billing.stripe_gateway.set_subscription_discount",
        lambda **kw: None,
    )

    user = _make_user(tier="free", credits=30)
    referrals = FakeReferralRepo(pending={"cs_ref_1"}, confirmed_count=3)
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users)

    await service.process(
        _event("checkout.session.completed", _referral_checkout(referrer.id))
    )

    assert sent == {
        "to": referrer.id,
        "percent": referrer_percent_off(3),
        "count": 3,
    }


@pytest.mark.asyncio
async def test_season_purchase_ignores_referral_metadata(captured_discounts):
    """Season passes are one-time payments: no recurring invoice for a recurring
    referrer reward to attach to. The season branch must not pay one."""
    user = _make_user(tier="free", credits=30)
    referrals = FakeReferralRepo(pending={"cs_season_ref"}, confirmed_count=1)
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users)

    obj = {
        "id": "cs_season_ref",
        "customer": "cus_1",
        "mode": "payment",
        "metadata": {
            "tier": "pro", "interval": "season",
            "redeemed_kind": "referral",
            "referrer_user_id": str(referrer.id),
        },
    }
    await service.process(_event("checkout.session.completed", obj))

    assert user.tier == "pro"
    assert captured_discounts == []
    assert referrals.recorded == []


@pytest.mark.asyncio
async def test_subscription_deleted_lowers_the_referrers_rate(captured_discounts):
    """A referral only pays for as long as the referred account keeps paying.
    Otherwise five throwaway accounts buy a permanent recurring discount."""
    from backend.models.user import referrer_percent_off

    user = _make_user(tier="standard", credits=0)
    user.stripe_subscription_id = "sub_1"
    referrer_id = uuid.uuid4()
    referrals = FakeReferralRepo(
        # This user was referred; after their downgrade the referrer is down to 1.
        redemption=SimpleNamespace(referrer_user_id=referrer_id),
        confirmed_count=1,
    )
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users)
    service._test_users.by_id[referrer_id] = referrer

    await service.process(
        _event("customer.subscription.deleted", _sub_obj(status="canceled"))
    )

    assert user.tier == "free"
    assert captured_discounts[0]["sub_id"] == "sub_referrer"
    assert captured_discounts[0]["coupon_id"].endswith(str(referrer_percent_off(1)))


@pytest.mark.asyncio
async def test_subscription_deleted_clears_the_coupon_at_zero(captured_discounts):
    """The last referral went away. There is no zero-percent coupon object, so
    the discount is cleared outright."""
    user = _make_user(tier="standard", credits=0)
    user.stripe_subscription_id = "sub_1"
    referrer_id = uuid.uuid4()
    referrals = FakeReferralRepo(
        redemption=SimpleNamespace(referrer_user_id=referrer_id), confirmed_count=0
    )
    service, _db = _build(user, referrals)
    referrer = _referrer(service._test_users)
    service._test_users.by_id[referrer_id] = referrer

    await service.process(
        _event("customer.subscription.deleted", _sub_obj(status="canceled"))
    )

    assert captured_discounts[0]["coupon_id"] is None


@pytest.mark.asyncio
async def test_subscription_deleted_without_a_referral_touches_nothing(
    captured_discounts,
):
    user = _make_user(tier="standard", credits=0)
    user.stripe_subscription_id = "sub_1"
    service, _db = _build(user, FakeReferralRepo(redemption=None))

    await service.process(
        _event("customer.subscription.deleted", _sub_obj(status="canceled"))
    )

    assert user.tier == "free"
    assert captured_discounts == []


@pytest.mark.asyncio
async def test_a_failing_rate_recompute_does_not_fail_the_downgrade(monkeypatch):
    """This is a correction to somebody else's subscription. Failing over it
    would roll back THIS user's downgrade and make Stripe redeliver forever."""
    class _Exploding(FakeReferralRepo):
        async def redemption_for_redeemer(self, redeemer_user_id):
            raise RuntimeError("the query failed")

    user = _make_user(tier="standard", credits=0)
    user.stripe_subscription_id = "sub_1"
    service, db = _build(user, _Exploding())

    result = await service.process(
        _event("customer.subscription.deleted", _sub_obj(status="canceled"))
    )

    assert result.handled
    assert user.tier == "free"
    db.commit.assert_awaited_once()


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
async def test_unmatched_customer_retries_and_is_not_recorded():
    """An entitlement event whose customer resolves to no user must NOT be
    recorded (so Stripe redelivers) — no silent 200-drop of a real payment."""
    user = _make_user(tier="standard", credits=100, customer_id="cus_1")
    service, db = _build(user)

    obj = {"id": "cs_x", "customer": "cus_OTHER", "mode": "payment",
           "metadata": {"pack": "credits_100", "credits": "100"}}
    result = await service.process(_event("checkout.session.completed", obj))

    assert result.retry is True
    assert not result.handled
    assert user.credits_remaining == 100          # side effect NOT applied
    db.commit.assert_not_awaited()                # NOT recorded as processed
    db.rollback.assert_awaited_once()             # rolled back for clean redelivery
    assert service._events.committed == set()     # event id not durably kept


@pytest.mark.asyncio
async def test_replay_of_failed_event_processes_normally():
    """A previously-failed (unmatched, rolled-back) event, redelivered after the
    user is resolvable, must reprocess — the event id was not burned."""
    user = _make_user(tier="free", credits=30, customer_id="cus_1")
    service, db = _build(user)

    ev = _event(
        "checkout.session.completed",
        {"id": "cs_r", "customer": "cus_LATE", "mode": "subscription",
         "subscription": "sub_9", "metadata": {"tier": "pro"}},
        event_id="evt_replay",
    )
    r1 = await service.process(ev)
    assert r1.retry is True and user.tier == "free"   # first delivery: no user yet

    user.stripe_customer_id = "cus_LATE"              # user now resolvable
    r2 = await service.process(ev)                    # SAME event id redelivered
    assert r2.handled and not r2.duplicate            # reprocessed, not skipped
    assert user.tier == "pro"


@pytest.mark.asyncio
async def test_no_customer_id_is_recorded_noop():
    """An event with NO customer id is 'not applicable' (nothing to match) — it is
    recorded + handled (no retry), distinct from an unmatched-but-present customer."""
    user = _make_user(tier="standard", credits=100, customer_id="cus_1")
    service, db = _build(user)

    obj = {"id": "cs_none", "mode": "payment",
           "metadata": {"pack": "credits_100", "credits": "100"}}  # no "customer"
    result = await service.process(_event("checkout.session.completed", obj))

    assert result.handled and not result.retry
    assert user.credits_remaining == 100          # no side effect (no user)
    db.commit.assert_awaited_once()               # recorded — retry would not help
