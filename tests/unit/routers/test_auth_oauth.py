"""Tests for Yahoo OAuth multi-user flow."""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


def _make_user(uid=None):
    user = MagicMock(spec=User)
    user.id = uid or uuid.uuid4()
    user.external_id = "clerk-test"
    user.email = "test@test.com"
    user.tier = "standard"
    return user


@pytest.mark.asyncio
async def test_yahoo_connect_redirects():
    user = _make_user()
    from backend.core.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: user

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            resp = await ac.get("/auth/yahoo/connect")
        # Should be a redirect to Yahoo
        assert resp.status_code in (302, 307)
        location = resp.headers.get("location", "")
        assert "yahoo.com" in location or "api.login.yahoo.com" in location
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_state_param_encodes_user_id():
    user = _make_user()
    from backend.core.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: user

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            resp = await ac.get("/auth/yahoo/connect")
        location = resp.headers.get("location", "")
        # Extract state param
        assert "state=" in location
        state_encoded = location.split("state=")[1].split("&")[0]
        state_data = json.loads(
            base64.urlsafe_b64decode(state_encoded).decode()
        )
        assert state_data["user_id"] == str(user.id)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_without_state_raises_error():
    from backend.core.dependencies import get_db
    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/auth/yahoo/callback?code=abc")
        # No state → ValidationError (422)
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_with_invalid_state_raises_error():
    from backend.core.dependencies import get_db
    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.get(
                "/auth/yahoo/callback?code=abc&state=not-valid-base64!!!"
            )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_yahoo_disconnect_removes_credentials():
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.routers.auth.CredentialRepository"
    ) as MockRepo:
        mock_repo = AsyncMock()
        MockRepo.return_value = mock_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.delete("/auth/yahoo/disconnect")
            assert resp.status_code == 200
            assert resp.json()["status"] == "disconnected"
            mock_repo.disconnect.assert_called_once_with(
                user.id, "yahoo"
            )
        finally:
            app.dependency_overrides.clear()
