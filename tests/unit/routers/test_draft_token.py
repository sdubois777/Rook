"""Tests for draft token and extension relay endpoints."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


def _make_user(uid=None, draft_token=None):
    user = MagicMock(spec=User)
    user.id = uid or uuid.uuid4()
    user.external_id = "clerk-test"
    user.email = "test@test.com"
    user.tier = "standard"
    user.draft_token = draft_token
    user.credits_remaining = 50
    return user


# ---------------------------------------------------------------------------
# Draft token endpoints (in /account)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_token_created_for_new_user():
    """GET /account/draft-token creates a token when user has none."""
    user = _make_user(draft_token=None)
    # db.get() returns a separate db_user object that the endpoint mutates
    db_user = _make_user(draft_token=None)
    db_user.id = user.id
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=db_user)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/account/draft-token")
        assert resp.status_code == 200
        token = resp.json()["draft_token"]
        assert token is not None
        assert db_user.draft_token == token
        mock_db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_token_stable_on_repeat_call():
    """GET /account/draft-token returns existing token without regenerating."""
    existing_token = str(uuid.uuid4())
    user = _make_user(draft_token=existing_token)
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/account/draft-token")
        assert resp.status_code == 200
        assert resp.json()["draft_token"] == existing_token
        # Should NOT have committed (no change)
        mock_db.commit.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_revoke_generates_new_token():
    """POST /account/draft-token/revoke generates a new token."""
    old_token = str(uuid.uuid4())
    user = _make_user(draft_token=old_token)
    db_user = _make_user(draft_token=old_token)
    db_user.id = user.id
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=db_user)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.post("/account/draft-token/revoke")
        assert resp.status_code == 200
        new_token = resp.json()["draft_token"]
        assert new_token != old_token
        assert db_user.draft_token == new_token
        mock_db.commit.assert_called_once()
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Draft event relay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_draft_event_rejects_invalid_token():
    """POST /draft/event with invalid token returns 401."""
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo:
        mock_repo = AsyncMock()
        mock_repo.get_by_draft_token.return_value = None
        MockRepo.return_value = mock_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/draft/event",
                    json={
                        "type": "nomination",
                        "platform": "yahoo",
                        "payload": {},
                    },
                    headers={"X-Draft-Token": "invalid-token"},
                )
            assert resp.status_code == 401
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_event_relays_to_ws_manager():
    """POST /draft/event with valid token broadcasts to WS manager."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo, patch(
        "backend.routers.draft.ws_manager"
    ) as mock_ws:
        mock_repo = AsyncMock()
        mock_repo.get_by_draft_token.return_value = user
        MockRepo.return_value = mock_repo
        mock_ws.broadcast = AsyncMock()

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/draft/event",
                    json={
                        "type": "nomination",
                        "platform": "yahoo",
                        "payload": {"player": "CMC"},
                    },
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            assert resp.json()["status"] == "relayed"
            mock_ws.broadcast.assert_called_once()
            call_data = mock_ws.broadcast.call_args[0][0]
            assert call_data["type"] == "nomination"
            assert call_data["platform"] == "yahoo"
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Passive sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_platform_skips_sleeper():
    """POST /leagues/sync-platform/sleeper returns skipped."""
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.post(
                "/leagues/sync-platform/sleeper",
                headers={"X-Draft-Token": "any"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"
        assert resp.json()["reason"] == "platform_excluded"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_platform_skips_invalid_token():
    """POST /leagues/sync-platform/yahoo with bad token returns 200 + skipped."""
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo:
        mock_repo = AsyncMock()
        mock_repo.get_by_draft_token.return_value = None
        MockRepo.return_value = mock_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/leagues/sync-platform/yahoo",
                    headers={"X-Draft-Token": "invalid"},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "skipped"
            assert data["reason"] == "invalid_token"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_platform_skips_no_leagues():
    """POST /leagues/sync-platform/espn with valid token but no leagues returns skipped."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockUserRepo, patch(
        "backend.routers.league_connect.LeagueRepository"
    ) as MockLeagueRepo:
        mock_user_repo = AsyncMock()
        mock_user_repo.get_by_draft_token.return_value = user
        MockUserRepo.return_value = mock_user_repo

        mock_league_repo = AsyncMock()
        mock_league_repo.get_user_leagues_by_platform.return_value = []
        MockLeagueRepo.return_value = mock_league_repo

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/leagues/sync-platform/espn",
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "skipped"
            assert data["reason"] == "no_leagues"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sync_platform_syncs_all_user_leagues():
    """POST /leagues/sync-platform/yahoo syncs all user's Yahoo leagues."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_league = MagicMock()
    mock_league.id = uuid.uuid4()

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockUserRepo, patch(
        "backend.routers.league_connect.LeagueRepository"
    ) as MockLeagueRepo, patch(
        "backend.services.league_sync.LeagueSyncService"
    ) as MockSync:
        mock_user_repo = AsyncMock()
        mock_user_repo.get_by_draft_token.return_value = user
        MockUserRepo.return_value = mock_user_repo

        mock_league_repo = AsyncMock()
        mock_league_repo.get_user_leagues_by_platform.return_value = [mock_league]
        MockLeagueRepo.return_value = mock_league_repo

        mock_sync = AsyncMock()
        MockSync.return_value = mock_sync

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/leagues/sync-platform/yahoo",
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["leagues_synced"] == 1
            mock_sync.sync_league.assert_called_once_with(mock_league.id)
        finally:
            app.dependency_overrides.clear()
