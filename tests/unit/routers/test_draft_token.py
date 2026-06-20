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
            resp = await ac.get("/api/account/draft-token")
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
            resp = await ac.get("/api/account/draft-token")
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
            resp = await ac.post("/api/account/draft-token/revoke")
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
                    "/api/draft/event",
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
    ) as mock_ws, patch(
        "backend.routers.draft._engine", AsyncMock()
    ):
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
                    "/api/draft/event",
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
# Live draft engine wiring (nomination -> recommendation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nomination_triggers_engine():
    """A nomination event resolves the player and runs the engine."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_player = MagicMock()
    fake_player.yahoo_player_id = "nfl_123"
    fake_player.position = "TE"
    engine = AsyncMock()

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo, patch(
        "backend.routers.draft.ws_manager"
    ) as mock_ws, patch(
        "backend.routers.draft._engine", engine
    ), patch(
        "backend.routers.draft._resolve_player",
        AsyncMock(return_value=fake_player),
    ):
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
                    "/api/draft/event",
                    json={
                        "type": "nomination",
                        "platform": "yahoo",
                        "payload": {
                            "player_name": "Sam LaPorta",
                            "opening_bid": 4,
                        },
                    },
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            engine.on_nomination.assert_awaited_once()
            sent = engine.on_nomination.call_args[0][0]
            assert sent["player_id"] == "nfl_123"
            assert sent["player_name"] == "Sam LaPorta"
            # Raw nomination still relayed to the UI
            raw = mock_ws.broadcast.call_args[0][0]
            assert raw["type"] == "nomination"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_bid_update_relayed_without_engine_call():
    """bid_update relays to the UI but never invokes the engine handlers."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    engine = AsyncMock()

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo, patch(
        "backend.routers.draft.ws_manager"
    ) as mock_ws, patch(
        "backend.routers.draft._engine", engine
    ):
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
                    "/api/draft/event",
                    json={
                        "type": "bid_update",
                        "platform": "yahoo",
                        "payload": {"player_name": "Sam LaPorta", "current_bid": 6},
                    },
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            engine.on_nomination.assert_not_awaited()
            engine.on_pick_confirmed.assert_not_awaited()
            raw = mock_ws.broadcast.call_args[0][0]
            assert raw["type"] == "bid_update"
            assert raw["payload"]["current_bid"] == 6
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_pick_recorded():
    """draft_pick records the pick into engine state and relays raw."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    fake_player = MagicMock()
    fake_player.yahoo_player_id = "nfl_9"
    fake_player.position = "RB"
    engine = AsyncMock()

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo, patch(
        "backend.routers.draft.ws_manager"
    ) as mock_ws, patch(
        "backend.routers.draft._engine", engine
    ), patch(
        "backend.routers.draft._resolve_player",
        AsyncMock(return_value=fake_player),
    ):
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
                    "/api/draft/event",
                    json={
                        "type": "draft_pick",
                        "platform": "yahoo",
                        "payload": {
                            "player_name": "Bijan Robinson",
                            "final_price": 20,
                            "winner": "Stephen",
                            "teams_snapshot": {},
                        },
                    },
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            engine.on_pick_confirmed.assert_awaited_once()
            sent = engine.on_pick_confirmed.call_args[0][0]
            assert sent["player_id"] == "nfl_9"
            assert sent["team_id"] == "Stephen"
            assert sent["final_price"] == 20
            raw = mock_ws.broadcast.call_args[0][0]
            assert raw["type"] == "draft_pick"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_nomination_lazy_inits_engine_when_none():
    """A nomination with no engine lazily builds one, then runs it."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    build = AsyncMock()
    trigger = AsyncMock()

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo, patch(
        "backend.routers.draft.ws_manager"
    ) as mock_ws, patch(
        "backend.routers.draft._engine", None
    ), patch(
        "backend.routers.draft._build_engine", build
    ), patch(
        "backend.routers.draft._trigger_nomination", trigger
    ):
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
                    "/api/draft/event",
                    json={
                        "type": "nomination",
                        "platform": "yahoo",
                        "payload": {"player_name": "Sam LaPorta"},
                    },
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            build.assert_awaited_once()
            trigger.assert_awaited_once()
            mock_ws.broadcast.assert_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_nomination_skips_lazy_init_when_engine_exists():
    """A nomination with an existing engine does NOT rebuild it."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    build = AsyncMock()
    trigger = AsyncMock()

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo, patch(
        "backend.routers.draft.ws_manager"
    ) as mock_ws, patch(
        "backend.routers.draft._engine", AsyncMock()
    ), patch(
        "backend.routers.draft._build_engine", build
    ), patch(
        "backend.routers.draft._trigger_nomination", trigger
    ):
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
                    "/api/draft/event",
                    json={
                        "type": "nomination",
                        "platform": "yahoo",
                        "payload": {"player_name": "Sam LaPorta"},
                    },
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            build.assert_not_awaited()
            trigger.assert_awaited_once()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_draft_pick_does_not_lazy_init():
    """A draft_pick with no engine relays raw but never builds/records."""
    user = _make_user(draft_token="valid-token")
    from backend.core.dependencies import get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    build = AsyncMock()
    record = AsyncMock()

    with patch(
        "backend.repositories.user_repo.UserRepository"
    ) as MockRepo, patch(
        "backend.routers.draft.ws_manager"
    ) as mock_ws, patch(
        "backend.routers.draft._engine", None
    ), patch(
        "backend.routers.draft._build_engine", build
    ), patch(
        "backend.routers.draft._record_pick", record
    ):
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
                    "/api/draft/event",
                    json={
                        "type": "draft_pick",
                        "platform": "yahoo",
                        "payload": {"player_name": "X", "final_price": 5},
                    },
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            build.assert_not_awaited()
            record.assert_not_awaited()
            mock_ws.broadcast.assert_awaited()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_start_draft_builds_engine_and_returns_ready():
    """POST /draft/start builds the engine via the shared helper, no Playwright."""
    build = AsyncMock()

    with patch(
        "backend.routers.draft._engine", None
    ), patch(
        "backend.routers.draft._bridge", None
    ), patch(
        "backend.routers.draft._build_engine", build
    ):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/api/draft/start",
                    json={"your_team_id": "team_5"},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ready"
            assert data["mode"] == "extension"
            # (your_team_id, league_id, draft_type) — draft_type omitted -> None
            build.assert_awaited_once_with("team_5", None, None)
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
                "/api/leagues/sync-platform/sleeper",
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
                    "/api/leagues/sync-platform/yahoo",
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
                    "/api/leagues/sync-platform/espn",
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
                    "/api/leagues/sync-platform/yahoo",
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["leagues_synced"] == 1
            mock_sync.sync_league.assert_called_once_with(mock_league.id)
        finally:
            app.dependency_overrides.clear()
