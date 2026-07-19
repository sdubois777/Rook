"""
Tests for the /webhooks/stripe route — signature verification posture.

The §4 dispatch logic is covered in test_webhook_service.py; here we verify the
HTTP boundary: an unverified payload is rejected with 400 and NO side effects, and
the dev-unverified path parses + dispatches the event.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


def _override_db():
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: MagicMock()


@pytest.mark.asyncio
async def test_invalid_signature_returns_400_no_side_effects(monkeypatch):
    from backend.config import settings
    from backend.services.billing import stripe_gateway

    monkeypatch.setattr(settings, "environment", "production", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test", raising=False)

    def _raise(*_a, **_k):
        raise ValueError("bad signature")

    monkeypatch.setattr(stripe_gateway, "construct_event", _raise)

    _override_db()
    with patch(
        "backend.services.billing.webhook_service.StripeWebhookService.from_session"
    ) as from_session:
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/webhooks/stripe",
                    content=b"{}",
                    headers={"stripe-signature": "t=1,v1=deadbeef"},
                )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 400
    # No entitlement path ever constructed → no side effects.
    from_session.assert_not_called()


@pytest.mark.asyncio
async def test_missing_secret_in_production_returns_400(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "environment", "production", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", None, raising=False)

    _override_db()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post("/webhooks/stripe", content=b"{}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_verified_path_dispatches_plain_dict_from_body(monkeypatch):
    """Prod path: construct_event verifies the signature (returns a StripeObject
    with no usable `.get`), so the route dispatches a plain dict parsed from the raw
    body — the handler's `.get()` dispatch depends on it (prod-path regression)."""
    import json as _json

    from backend.config import settings
    from backend.services.billing import stripe_gateway

    monkeypatch.setattr(settings, "environment", "production", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test", raising=False)
    # Signature "verifies" (no raise); its return value must be ignored by the route.
    monkeypatch.setattr(stripe_gateway, "construct_event",
                        lambda *a, **k: object())

    event = {"id": "evt_obj", "type": "checkout.session.completed",
             "data": {"object": {"customer": "cus_1", "mode": "subscription"}}}

    fake_service = MagicMock()
    fake_service.process = AsyncMock(
        return_value=MagicMock(event_type="checkout.session.completed",
                               duplicate=False, handled=True, retry=False)
    )

    _override_db()
    with patch(
        "backend.services.billing.webhook_service.StripeWebhookService.from_session",
        return_value=fake_service,
    ):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/webhooks/stripe", content=_json.dumps(event).encode(),
                    headers={"stripe-signature": "t=1,v1=abc"},
                )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    dispatched = fake_service.process.await_args.args[0]
    assert isinstance(dispatched, dict)  # plain dict, not a StripeObject
    assert dispatched.get("id") == "evt_obj"  # .get() works
    assert dispatched["data"]["object"].get("customer") == "cus_1"


@pytest.mark.asyncio
async def test_dev_unverified_path_requires_explicit_optin(monkeypatch):
    """No signature: the unverified parse runs ONLY with the explicit dual opt-in
    (allow-unverified var True AND non-production). With it, the event dispatches."""
    from backend.config import settings
    monkeypatch.setattr(settings, "environment", "development", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", None, raising=False)
    monkeypatch.setattr(settings, "stripe_allow_unverified_webhooks", True, raising=False)

    fake_service = MagicMock()
    fake_service.process = AsyncMock(
        return_value=MagicMock(event_type="checkout.session.completed",
                               duplicate=False, handled=True, retry=False)
    )

    _override_db()
    with patch(
        "backend.services.billing.webhook_service.StripeWebhookService.from_session",
        return_value=fake_service,
    ):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/webhooks/stripe",
                    json={"id": "evt_1", "type": "checkout.session.completed",
                          "data": {"object": {}}},
                )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    fake_service.process.assert_awaited_once()
    dispatched = fake_service.process.await_args.args[0]
    assert dispatched["id"] == "evt_1"


async def _post_no_service(monkeypatch, *, env, secret, allow, headers=None, body=None):
    """POST to /webhooks/stripe with no service patched (verification should reject
    before the service is ever built). Returns the status code."""
    from backend.config import settings
    monkeypatch.setattr(settings, "environment", env, raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", secret, raising=False)
    monkeypatch.setattr(settings, "stripe_allow_unverified_webhooks", allow, raising=False)
    _override_db()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/webhooks/stripe",
                content=(body if body is not None else b'{"id":"evt_x","type":"x","data":{"object":{}}}'),
                headers=headers or {},
            )
    finally:
        app.dependency_overrides.clear()
    return resp.status_code


@pytest.mark.parametrize("env", ["development", "production"])
@pytest.mark.asyncio
async def test_no_signature_no_optin_rejected_every_env(monkeypatch, env):
    """No signature + opt-in absent → 400, in EVERY environment (fail closed)."""
    status = await _post_no_service(monkeypatch, env=env, secret=None, allow=False)
    assert status == 400


@pytest.mark.parametrize("env", ["development", "production"])
@pytest.mark.asyncio
async def test_present_but_invalid_signature_400_every_env(monkeypatch, env):
    """A PRESENT signature is always verified; invalid → hard 400 (never falls
    through to unverified parsing), even with the unverified opt-in set."""
    from backend.services.billing import stripe_gateway

    def _raise(*a, **k):
        raise ValueError("bad sig")
    monkeypatch.setattr(stripe_gateway, "construct_event", _raise, raising=False)
    status = await _post_no_service(
        monkeypatch, env=env, secret="whsec_test", allow=True,
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert status == 400


@pytest.mark.asyncio
async def test_unmatched_customer_returns_500_for_retry(monkeypatch):
    """A retry result (unmatched customer) → 500 so Stripe redelivers."""
    import json as _json
    from backend.config import settings
    monkeypatch.setattr(settings, "environment", "production", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test", raising=False)
    from backend.services.billing import stripe_gateway
    monkeypatch.setattr(stripe_gateway, "construct_event", lambda *a, **k: object())

    fake_service = MagicMock()
    fake_service.process = AsyncMock(
        return_value=MagicMock(event_type="checkout.session.completed",
                               duplicate=False, handled=False, retry=True)
    )
    _override_db()
    with patch(
        "backend.services.billing.webhook_service.StripeWebhookService.from_session",
        return_value=fake_service,
    ):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/webhooks/stripe",
                    content=_json.dumps({"id": "evt_r", "type": "checkout.session.completed",
                                         "data": {"object": {}}}).encode(),
                    headers={"stripe-signature": "t=1,v1=abc"},
                )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 500
