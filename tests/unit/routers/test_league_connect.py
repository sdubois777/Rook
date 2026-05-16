"""Tests for league connect router endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


def _make_user(tier="standard", uid=None):
    user = MagicMock(spec=User)
    user.id = uid or uuid.uuid4()
    user.external_id = "clerk-test"
    user.email = "test@test.com"
    user.tier = tier
    user.credits_remaining = 100
    return user


def _make_league(user_id, platform="yahoo"):
    league = MagicMock()
    league.id = uuid.uuid4()
    league.user_id = user_id
    league.platform = platform
    league.league_id = "test-league"
    league.season_year = 2026
    league.team_count = 12
    league.draft_type = "auction"
    league.scoring = "ppr"
    league.budget = 200
    league.is_active = True
    league.last_synced = None
    league.manager_map = None
    return league


@pytest.mark.asyncio
async def test_connect_sleeper_validates_username():
    """Sleeper connect with non-existent username returns 404."""
    user = _make_user()

    from backend.core.dependencies import get_current_user, get_db
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    # Mock httpx.AsyncClient used inside the endpoint function
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.json.return_value = None

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response

    with patch("httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client
        )
        MockAsyncClient.return_value.__aexit__ = AsyncMock(
            return_value=False
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/leagues/connect/sleeper",
                    json={"username": "nonexistent", "league_id": "123"},
                )
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_callback_requires_cookies():
    """ESPN callback without cookies returns error."""
    user = _make_user()

    from backend.core.dependencies import get_current_user, get_db
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            # Missing espn_s2 and swid params
            resp = await ac.get("/leagues/connect/espn/callback")
        # FastAPI will return 422 for missing required query params
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_callback_requires_auth():
    """ESPN bookmarklet callback requires authenticated user."""
    from backend.core.dependencies import get_current_user

    # Override auth to raise — simulates unauthenticated request
    async def raise_unauth():
        from backend.core.exceptions import UnauthorizedError
        raise UnauthorizedError("Not authenticated")

    app.dependency_overrides[get_current_user] = raise_unauth

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            resp = await ac.get(
                "/leagues/connect/espn/callback"
                "?espn_s2=test_cookie&swid=test_swid"
            )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_invalid_espn_cookies_raise_app_error():
    """ESPN connect with invalid cookies returns error."""
    user = _make_user()

    from backend.core.dependencies import get_current_user, get_db
    from backend.core.exceptions import AppError

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    # ESPNLeagueAPI is lazy-imported inside connect_espn_league endpoint
    mock_api = AsyncMock()
    mock_api.validate_cookies.side_effect = AppError(
        "ESPN cookies expired — please reconnect"
    )

    mock_espn_cls = MagicMock(return_value=mock_api)

    with patch(
        "backend.integrations.espn_league_api.ESPNLeagueAPI",
        mock_espn_cls,
    ):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/leagues/connect/espn",
                    json={
                        "league_id": "12345",
                        "espn_s2": "bad_cookie",
                        "swid": "bad_swid",
                    },
                )
            # AppError returns 400 or custom status
            assert resp.status_code in (400, 401, 422, 500)
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_league_status_returns_info():
    user = _make_user()
    league = _make_league(user.id)
    league.last_synced = datetime(2026, 5, 1, tzinfo=timezone.utc)

    from backend.core.dependencies import get_current_user, get_db
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.routers.league_connect._get_user_league",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = league

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.get(
                    f"/leagues/{league.id}/status"
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["platform"] == league.platform
            assert data["is_active"] is True
            assert data["last_synced"] is not None
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_disconnect_league():
    user = _make_user()
    league_id = uuid.uuid4()

    from backend.core.dependencies import get_current_user, get_db
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.services.league_service.LeagueService"
    ) as MockService:
        mock_service = AsyncMock()
        MockService.return_value = mock_service

        # Also need to patch the lazy import inside the endpoint
        with patch.dict(
            "sys.modules",
            {"backend.services.league_service": MagicMock(
                LeagueService=MockService
            )},
        ):
            try:
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                ) as ac:
                    resp = await ac.delete(f"/leagues/{league_id}")
                assert resp.status_code == 200
                assert resp.json()["status"] == "disconnected"
            finally:
                app.dependency_overrides.clear()
