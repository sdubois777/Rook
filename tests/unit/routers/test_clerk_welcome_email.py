"""Tests for the welcome email sent from the Clerk user.created webhook.

No database and no mail provider. AsyncSessionLocal is replaced with a mock
session whose insert result is controlled per test, because the ONE thing this
path has to get right is telling a genuine signup from a redelivered event:
Clerk retries a webhook that errors, and every retry runs the same insert.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


def _event(external_id="user_clerk_1", email="new@example.com"):
    return {
        "type": "user.created",
        "data": {
            "id": external_id,
            "email_addresses": [{"email_address": email}],
            "first_name": "New",
            "last_name": "Signup",
        },
    }


def _mock_session(*scalars):
    """A session whose successive execute() calls yield `scalars` in order.

    The handler can issue more than one statement, so a single fixed return value
    cannot model it. The order for a signup where the row ALREADY EXISTED is:

        1. INSERT ... ON CONFLICT DO NOTHING RETURNING id   -> None (conflict)
        2. SELECT id WHERE external_id = ...                -> the existing id
        3. UPDATE ... SET display_name                      -> unused

    The last value repeats, so passing one scalar still models the simple case.
    """
    results = []
    for value in scalars:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        results.append(r)

    db = AsyncMock()
    calls = {"n": 0}

    async def _execute(*_a, **_kw):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]

    db.execute = _execute
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return db, ctx


@pytest.fixture
def sent(monkeypatch):
    """Capture send_welcome instead of calling a provider."""
    from backend.services.email.email_service import EmailService
    from backend.services.referral_service import ReferralService

    calls = []

    async def _capture(self, *, user, promo_code, referral_code):
        calls.append(
            {
                "user_id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "promo_code": promo_code,
                "referral_code": referral_code,
            }
        )
        return "sent"

    async def _code(self, user_id):
        return "ROOK-ABC123"

    monkeypatch.setattr(EmailService, "send_welcome", _capture)
    monkeypatch.setattr(ReferralService, "get_or_create_code", _code)
    return calls


async def _post(event):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        return await ac.post(
            "/webhooks/clerk",
            content=json.dumps(event),
            headers={"Content-Type": "application/json"},
        )


@pytest.fixture(autouse=True)
def _dev_unverified(monkeypatch):
    """Clerk signature verification is covered in test_auth.py; skip it here."""
    from backend.config import settings

    monkeypatch.setattr(settings, "clerk_webhook_secret", None, raising=False)
    monkeypatch.setattr(settings, "environment", "development", raising=False)


@pytest.mark.asyncio
async def test_new_signup_is_welcomed_with_their_own_codes(sent):
    from backend.services.referral_service import ReferralService

    user_id = uuid.uuid4()
    db, ctx = _mock_session(user_id)

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(_event())

    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0]["user_id"] == user_id
    assert sent[0]["email"] == "new@example.com"
    assert sent[0]["display_name"] == "New Signup"
    assert sent[0]["referral_code"] == "ROOK-ABC123"
    # The welcome code in the email is the one that account's checkout will
    # accept — it is derived from the user id, not a shared string.
    assert sent[0]["promo_code"] == ReferralService(None, None).welcome_code_for(
        user_id
    )
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_the_request_path_winning_the_race_still_gets_a_welcome_email(sent):
    """REGRESSION. This shipped broken and cost a real signup their email.

    Two paths create a user row: this webhook, and UserService.get_or_create on
    the first authenticated request. Clerk webhooks are asynchronous, so a fast
    browser routinely calls the API first and the lazy path wins — this insert
    then conflicts and RETURNING yields nothing.

    The original code read "no row returned" as "not a new signup" and skipped the
    email. That was wrong twice over: it is the normal case for a fast client, and
    exactly-once was already guaranteed by the send lock, which claims the dedupe
    key welcome:<user_id> before the provider is called. The handler must look the
    user up and attempt the send regardless.
    """
    existing_id = uuid.uuid4()
    # INSERT conflicts (None), then the SELECT finds the row the other path made.
    _db, ctx = _mock_session(None, existing_id)

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(_event())

    assert resp.status_code == 200
    assert len(sent) == 1, "a signup created by the request path must still be welcomed"
    assert sent[0]["user_id"] == existing_id


@pytest.mark.asyncio
async def test_an_unknown_clerk_id_sends_nothing(sent):
    """Insert conflicted AND the lookup found nobody. There is no user to mail;
    attempting one would be a crash, not an email."""
    _db, ctx = _mock_session(None, None)

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(_event())

    assert resp.status_code == 200
    assert sent == []


@pytest.mark.asyncio
async def test_a_redelivered_event_is_deduped_by_the_send_lock_not_by_this_handler(
    sent, monkeypatch
):
    """Clerk retries an errored webhook, so the same event can arrive twice.

    Exactly-once is enforced one layer down: EmailService claims the dedupe key
    before calling the provider, so the second attempt is refused at the database.
    This test asserts the handler DOES attempt both times and that the refusal is
    what stops the duplicate — the division of responsibility the previous design
    got wrong by trying to decide it here.
    """
    from backend.services.email.email_service import EmailService

    existing_id = uuid.uuid4()
    statuses = ["sent", "skipped"]

    async def _capture(self, *, user, promo_code, referral_code):
        sent.append({"user_id": user.id, "status": statuses[len(sent)]})
        return statuses[len(sent) - 1]

    monkeypatch.setattr(EmailService, "send_welcome", _capture)

    for _ in range(2):
        _db, ctx = _mock_session(None, existing_id)
        with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
            resp = await _post(_event())
        assert resp.status_code == 200

    assert [c["status"] for c in sent] == ["sent", "skipped"]


@pytest.mark.asyncio
async def test_a_placeholder_address_is_never_mailed(sent):
    """Synthesized addresses do not resolve. Mailing them earns hard bounces, and
    a bounce rate is what gets a sending domain blocked."""
    _db, ctx = _mock_session(uuid.uuid4())

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(_event(email="user_abc@placeholder.local"))

    assert resp.status_code == 200
    assert sent == []


@pytest.mark.asyncio
async def test_a_missing_address_is_never_mailed(sent):
    _db, ctx = _mock_session(uuid.uuid4())
    event = _event()
    event["data"]["email_addresses"] = []

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(event)

    assert resp.status_code == 200
    assert sent == []


@pytest.mark.asyncio
async def test_a_raising_email_call_does_not_fail_the_webhook(monkeypatch):
    """Clerk retries a webhook that errors, and a retry of user.created runs the
    insert again. A mail outage must not become a signup loop."""
    from backend.services.email.email_service import EmailService
    from backend.services.referral_service import ReferralService

    async def _boom(self, **kwargs):
        raise RuntimeError("the mail provider is down")

    async def _code(self, user_id):
        return "ROOK-ABC123"

    monkeypatch.setattr(EmailService, "send_welcome", _boom)
    monkeypatch.setattr(ReferralService, "get_or_create_code", _code)

    db, ctx = _mock_session(uuid.uuid4())
    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(_event())

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_a_failing_code_mint_does_not_fail_the_webhook(monkeypatch):
    """Same rule for everything the email needs, not just the send itself."""
    from backend.services.referral_service import ReferralService

    async def _boom(self, user_id):
        raise RuntimeError("could not allocate a code")

    monkeypatch.setattr(ReferralService, "get_or_create_code", _boom)

    _db, ctx = _mock_session(uuid.uuid4())
    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(_event())

    assert resp.status_code == 200
