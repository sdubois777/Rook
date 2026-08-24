"""Who is getting which draft-event error (#466).

Diagnosing issue #461 meant reading a wall of identical
"POST /api/draft/event 403" lines with no way to tell whose they were, how many
accounts were involved, or whether the accounts were genuinely free or paid ones
whose entitlement had quietly expired.

The reason the request log could not answer it: RequestLoggingMiddleware reads
the user from an `X-User-Id` header that only exists in development
(backend/core/dependencies.py sets it when Clerk is disabled), so in production
every extension request logs `user=-`.

These tests pin the two things that close that gap: refusals are logged with the
account and the cause, and an admin endpoint reports who would be refused without
waiting for them to hit it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.routers import draft as draft_mod


# ---------------------------------------------------------------------------
# Throttle — identity must not cost a flood
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_state():
    draft_mod._refusal_log_state.clear()
    draft_mod._last_blocked_notice.clear()
    yield
    draft_mod._refusal_log_state.clear()
    draft_mod._last_blocked_notice.clear()


def test_first_refusal_logs_immediately_then_throttles():
    """A refused extension posts about once a second for a whole draft. The first
    line must appear at once; the rest must not bury it."""
    emit, suppressed = draft_mod._should_log_refusal("403:abc")
    assert emit is True and suppressed == 0

    for _ in range(50):
        emit, _ = draft_mod._should_log_refusal("403:abc")
        assert emit is False


def test_throttle_carries_the_suppressed_count_forward():
    """The RATE is itself diagnostic, so what the throttle drops is counted, not
    discarded."""
    draft_mod._should_log_refusal("403:abc")          # first line
    for _ in range(9):
        draft_mod._should_log_refusal("403:abc")

    # Force the window open without sleeping.
    draft_mod._refusal_log_state["403:abc"][0] -= draft_mod.REFUSAL_LOG_INTERVAL_S + 1
    emit, suppressed = draft_mod._should_log_refusal("403:abc")
    assert emit is True
    assert suppressed == 9


def test_throttle_is_per_key_so_accounts_do_not_mask_each_other():
    """One noisy refused account must not hide a second one — the whole point is
    knowing HOW MANY accounts are affected."""
    assert draft_mod._should_log_refusal("403:user-a")[0] is True
    assert draft_mod._should_log_refusal("403:user-b")[0] is True
    assert draft_mod._should_log_refusal("401:token-fp")[0] is True


# ---------------------------------------------------------------------------
# Token fingerprint — correlate without logging a live credential
# ---------------------------------------------------------------------------
def test_fingerprint_is_stable_and_distinguishes_tokens():
    """Tells "one stale token retried for hours" apart from "many failing", which
    are different problems with different responses."""
    assert draft_mod._token_fingerprint("tok-1") == draft_mod._token_fingerprint("tok-1")
    assert draft_mod._token_fingerprint("tok-1") != draft_mod._token_fingerprint("tok-2")


def test_fingerprint_never_contains_the_token():
    """A draft token is a live credential. It must never reach the logs — anyone
    reading them could otherwise post events as that user."""
    secret = "super-secret-draft-token-value"
    fp = draft_mod._token_fingerprint(secret)
    assert secret not in fp
    assert len(fp) == 12
    assert fp not in secret


# ---------------------------------------------------------------------------
# The refusal paths actually log
# ---------------------------------------------------------------------------
def _event():
    return draft_mod.DraftEventPayload(
        type="snake_status", platform="espn", payload={"current_pick": 11}
    )


def _patch_user(user):
    repo = MagicMock()
    repo.get_by_draft_token = AsyncMock(return_value=user)
    return patch("backend.repositories.user_repo.UserRepository", return_value=repo)


async def test_expired_paid_account_is_logged_with_both_tiers(caplog):
    """The field that matters: an account whose STORED plan says pro but whose
    entitlement expired. It looks paid on the account page and to the customer,
    and is refused anyway — exactly the state that made #461 hard to attribute."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tier = "pro"
    user.tier_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    user.subscription_status = "active"

    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock()
    with caplog.at_level("WARNING"), _patch_user(user), \
            patch.object(draft_mod, "ws_manager", ws):
        resp = await draft_mod.relay_draft_event(
            _event(), x_draft_token="tok", db=MagicMock()
        )

    assert resp.status_code == 403
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert str(user.id) in line              # WHO
    assert "stored_tier=pro" in line         # what the account claims
    assert "computed_tier=free" in line      # what is actually enforced
    assert "REFUSED 403" in line


async def test_unknown_token_is_logged_by_fingerprint_not_by_value(caplog):
    with caplog.at_level("WARNING"), _patch_user(None):
        with pytest.raises(Exception):
            await draft_mod.relay_draft_event(
                _event(), x_draft_token="a-stale-token", db=MagicMock()
            )

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "REFUSED 401" in line
    assert draft_mod._token_fingerprint("a-stale-token") in line
    assert "a-stale-token" not in line        # the credential itself never appears


async def test_an_accepted_event_logs_no_refusal(caplog):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tier = "standard"
    user.tier_expires_at = None
    user.subscription_status = "active"

    sm = MagicMock()
    sm.get_or_rehydrate = AsyncMock(return_value=None)
    sm.persist = AsyncMock()
    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock()

    with caplog.at_level("WARNING"), _patch_user(user), \
            patch.object(draft_mod, "ws_manager", ws), \
            patch.object(draft_mod, "session_manager", sm):
        await draft_mod.relay_draft_event(
            _event(), x_draft_token="tok", db=MagicMock()
        )

    assert "REFUSED" not in "\n".join(r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Admin endpoint — who WOULD be refused, without waiting for a complaint
# ---------------------------------------------------------------------------
def _u(tier, expires=None, status=None):
    u = MagicMock()
    u.id = uuid.uuid4()
    u.email = f"{tier}@example.test"
    u.tier = tier
    u.tier_expires_at = expires
    u.subscription_status = status
    u.draft_token = "tok"
    return u


async def test_admin_endpoint_separates_expired_paid_from_genuinely_free():
    """The two causes need opposite responses: an expired paid account is a
    billing problem for that customer, a free account is working as designed."""
    from backend.routers import admin as admin_mod

    past = datetime.now(timezone.utc) - timedelta(days=2)
    future = datetime.now(timezone.utc) + timedelta(days=30)
    users = [
        _u("pro", past, "active"),        # expired paid — refused, looks paid
        _u("standard", past, "active"),   # expired paid — refused, looks paid
        _u("free"),                       # free with the extension — expected
        _u("pro", future, "active"),      # still entitled — must not appear
        _u("standard", None, "active"),   # monthly, no expiry — must not appear
    ]
    result = MagicMock()
    result.scalars.return_value.all.return_value = users
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch.object(admin_mod, "AsyncSessionLocal", MagicMock(return_value=ctx)):
        resp = await admin_mod.get_live_draft_entitlement_mismatches()

    assert [m.stored_tier for m in resp.expired_paid] == ["pro", "standard"]
    assert all(m.computed_tier == "free" for m in resp.expired_paid)
    assert [m.stored_tier for m in resp.free_with_extension] == ["free"]

    # Entitled accounts appear in NEITHER list — a false alarm here would send an
    # operator chasing a customer who has no problem.
    listed = {m.user_id for m in resp.expired_paid + resp.free_with_extension}
    assert str(users[3].id) not in listed
    assert str(users[4].id) not in listed
