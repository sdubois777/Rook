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
                               duplicate=False, handled=True)
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
async def test_dev_unverified_path_dispatches_event(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "environment", "development", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", None, raising=False)

    fake_service = MagicMock()
    fake_service.process = AsyncMock(
        return_value=MagicMock(event_type="checkout.session.completed",
                               duplicate=False, handled=True)
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
