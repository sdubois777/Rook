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
async def test_state_param_decoded_on_callback():
    """Yahoo callback correctly decodes user_id from state and stores tokens."""
    user = _make_user()
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    # Encode state with user_id
    state = base64.urlsafe_b64encode(
        json.dumps({"user_id": str(user.id)}).encode()
    ).decode()

    mock_tokens = {
        "access_token": "new_access_token",
        "refresh_token": "new_refresh_token",
        "expires_in": 3600,
    }

    with patch(
        "backend.routers.auth.exchange_code_for_tokens",
        new_callable=AsyncMock,
        return_value=mock_tokens,
    ), patch(
        "backend.routers.auth.CredentialRepository"
    ) as MockRepo:
        mock_repo = AsyncMock()
        MockRepo.return_value = mock_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/auth/yahoo/callback?code=test_code&state={state}"
                )
            # Should redirect to /league-setup?platform=yahoo
            assert resp.status_code == 302
            assert "platform=yahoo" in resp.headers.get("location", "")

            # Verify upsert_yahoo called with decoded user_id
            mock_repo.upsert_yahoo.assert_called_once()
            call_kwargs = mock_repo.upsert_yahoo.call_args
            assert call_kwargs.kwargs.get("user_id") == str(user.id)
            assert call_kwargs.kwargs.get("access_token") == "new_access_token"
            assert call_kwargs.kwargs.get("refresh_token") == "new_refresh_token"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_yahoo_leagues_requires_connection():
    """GET /auth/yahoo/leagues returns 400 when Yahoo not connected."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.routers.auth.CredentialRepository"
    ) as MockRepo:
        mock_repo = AsyncMock()
        mock_repo.get_yahoo_tokens.return_value = None
        MockRepo.return_value = mock_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.get("/auth/yahoo/leagues")
            assert resp.status_code == 400
            assert resp.json()["action"] == "connect"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_yahoo_leagues_returns_list():
    """GET /auth/yahoo/leagues returns league list when connected."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    future_expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)
    mock_leagues = [
        {"league_key": "449.l.123", "league_id": "123", "name": "Test League",
         "season": "2026", "num_teams": 12, "draft_type": "auction",
         "scoring_type": "head", "is_finished": False, "logo_url": ""},
    ]

    with patch(
        "backend.routers.auth.CredentialRepository"
    ) as MockRepo, patch(
        "backend.routers.auth.get_user_leagues",
        new_callable=AsyncMock,
        return_value=mock_leagues,
    ):
        mock_repo = AsyncMock()
        mock_repo.get_yahoo_tokens.return_value = (
            "access_tok", "refresh_tok", future_expiry
        )
        MockRepo.return_value = mock_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.get("/auth/yahoo/leagues")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["leagues"]) == 1
            assert data["leagues"][0]["name"] == "Test League"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_yahoo_leagues_auto_refreshes_expired_token():
    """GET /auth/yahoo/leagues refreshes token when expired."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    # Token expired 10 minutes ago
    expired_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new_expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)

    with patch(
        "backend.routers.auth.CredentialRepository"
    ) as MockRepo, patch(
        "backend.routers.auth.refresh_access_token_for_user",
        new_callable=AsyncMock,
        return_value=("new_access", "new_refresh", new_expiry),
    ) as mock_refresh, patch(
        "backend.routers.auth.get_user_leagues",
        new_callable=AsyncMock,
        return_value=[],
    ):
        mock_repo = AsyncMock()
        mock_repo.get_yahoo_tokens.return_value = (
            "old_access", "old_refresh", expired_at
        )
        MockRepo.return_value = mock_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.get("/auth/yahoo/leagues")
            assert resp.status_code == 200

            # Refresh was called
            mock_refresh.assert_awaited_once_with("old_refresh")
            # New tokens stored
            mock_repo.upsert_yahoo.assert_awaited_once_with(
                user.id, "new_access", "new_refresh", new_expiry,
            )
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
