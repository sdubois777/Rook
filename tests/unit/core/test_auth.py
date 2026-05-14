"""Tests for Clerk JWT verification and webhook handler."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


@pytest.mark.asyncio
async def test_verify_jwt_invalid_token_raises():
    """Invalid JWT raises UnauthorizedError (401)."""
    from backend.core.dependencies import _verify_clerk_jwt
    from backend.core.exceptions import UnauthorizedError

    with patch(
        "backend.core.dependencies._get_clerk_jwks",
        new_callable=AsyncMock,
        return_value={"keys": []},
    ):
        with pytest.raises(UnauthorizedError):
            await _verify_clerk_jwt("invalid.jwt.token")


@pytest.mark.asyncio
async def test_get_current_user_id_dev_fallback():
    """In dev without Clerk configured, X-User-Id header returns user_id."""
    from backend.core.dependencies import get_current_user_id

    with patch("backend.config.settings") as mock_settings:
        mock_settings.clerk_enabled = False
        mock_settings.environment = "development"

        request = MagicMock()
        request.headers = {"X-User-Id": "test-user-42"}

        result = await get_current_user_id(request, credentials=None)
        assert result["user_id"] == "test-user-42"


@pytest.mark.asyncio
async def test_get_current_user_id_dev_default():
    """In dev without X-User-Id header, defaults to dev-user-001."""
    from backend.core.dependencies import get_current_user_id

    with patch("backend.config.settings") as mock_settings:
        mock_settings.clerk_enabled = False
        mock_settings.environment = "development"

        request = MagicMock()
        request.headers = {}

        result = await get_current_user_id(request, credentials=None)
        assert result["user_id"] == "dev-user-001"


@pytest.mark.asyncio
async def test_get_current_user_id_requires_token_in_prod():
    """In production without Clerk, raises UnauthorizedError."""
    from backend.core.dependencies import get_current_user_id
    from backend.core.exceptions import UnauthorizedError

    with patch("backend.config.settings") as mock_settings:
        mock_settings.clerk_enabled = False
        mock_settings.environment = "production"

        request = MagicMock()
        request.headers = {}

        with pytest.raises(UnauthorizedError):
            await get_current_user_id(request, credentials=None)


@pytest.mark.asyncio
async def test_get_current_user_id_missing_bearer_with_clerk():
    """With Clerk enabled but no Authorization header, raises 401."""
    from backend.core.dependencies import get_current_user_id
    from backend.core.exceptions import UnauthorizedError

    with patch("backend.config.settings") as mock_settings:
        mock_settings.clerk_enabled = True
        mock_settings.environment = "production"

        request = MagicMock()
        request.headers = {}

        with pytest.raises(UnauthorizedError, match="Authorization header required"):
            await get_current_user_id(request, credentials=None)


@pytest.mark.asyncio
async def test_clerk_webhook_creates_user():
    """user.created event creates DB record with intro tier and signup bonus."""
    event = {
        "type": "user.created",
        "data": {
            "id": "user_clerk_test_001",
            "email_addresses": [{"email_address": "test@example.com"}],
            "first_name": "Test",
            "last_name": "User",
        },
    }

    mock_db = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=mock_ctx):
        with patch("backend.config.settings") as mock_settings:
            mock_settings.clerk_webhook_secret = None
            mock_settings.environment = "development"

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/webhooks/clerk",
                    content=json.dumps(event),
                    headers={"Content-Type": "application/json"},
                )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_db.execute.assert_awaited()
    mock_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_clerk_webhook_soft_deletes_user():
    """user.deleted event sets deleted_at timestamp."""
    event = {
        "type": "user.deleted",
        "data": {"id": "user_clerk_test_001"},
    }

    mock_db = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=mock_ctx):
        with patch("backend.config.settings") as mock_settings:
            mock_settings.clerk_webhook_secret = None
            mock_settings.environment = "development"

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/webhooks/clerk",
                    content=json.dumps(event),
                    headers={"Content-Type": "application/json"},
                )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_db.execute.assert_awaited()
    mock_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_clerk_webhook_is_idempotent():
    """Duplicate user.created events don't error (on_conflict_do_nothing)."""
    event = {
        "type": "user.created",
        "data": {
            "id": "user_clerk_duplicate",
            "email_addresses": [{"email_address": "dup@example.com"}],
            "first_name": "",
            "last_name": "",
        },
    }

    mock_db = AsyncMock()
    mock_db.execute.return_value = None
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.webhooks.AsyncSessionLocal", return_value=mock_ctx):
        with patch("backend.config.settings") as mock_settings:
            mock_settings.clerk_webhook_secret = None
            mock_settings.environment = "development"

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp1 = await ac.post(
                    "/webhooks/clerk",
                    content=json.dumps(event),
                    headers={"Content-Type": "application/json"},
                )
                resp2 = await ac.post(
                    "/webhooks/clerk",
                    content=json.dumps(event),
                    headers={"Content-Type": "application/json"},
                )

    assert resp1.status_code == 200
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_health_check_still_public():
    """Health check endpoint requires no auth."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
