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


def _mock_session(inserted_id):
    """A session whose INSERT ... RETURNING yields `inserted_id`.

    None is what ON CONFLICT DO NOTHING returns on a conflict — the row already
    existed and this event is a redelivery.
    """
    result = MagicMock()
    result.scalar_one_or_none.return_value = inserted_id
    db = AsyncMock()
    db.execute.return_value = result
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
async def test_a_redelivered_user_created_sends_nothing(sent):
    """ON CONFLICT DO NOTHING returns no row, which is the only signal that the
    account already existed. Without it every Clerk retry re-mails the user."""
    _db, ctx = _mock_session(None)

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=ctx):
        resp = await _post(_event())

    assert resp.status_code == 200
    assert sent == []


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
