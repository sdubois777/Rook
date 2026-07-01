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
