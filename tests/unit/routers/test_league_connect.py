"""Tests for league connect router endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.exceptions import PlatformAuthError
from backend.main import app
from backend.models.user import User


def _make_user(tier="standard", uid=None):
    user = MagicMock(spec=User)
    user.id = uid or uuid.uuid4()
    user.external_id = "clerk-test"
    user.email = "test@test.com"
    user.tier = tier
    user.tier_expires_at = None
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
                    "/api/leagues/connect/sleeper",
                    json={"username": "nonexistent", "league_id": "123"},
                )
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_bookmarklet_callback_route_is_gone():
    """GET /leagues/connect/espn/callback was REMOVED and must stay removed.

    It accepted `espn_s2` and `swid` as URL QUERY PARAMETERS — session credentials
    in a place that lands in access logs, browser history and Referer headers. It
    was also unreachable: no frontend or extension code ever called it, and the
    "ESPN bookmarklet" it served is a UI that does not exist.

    This replaces two tests that asserted the route's 422/401 behaviour, i.e. that
    pinned a credentials-in-URL endpoint as correct. Asserting 404 is the point.
    """
    user = _make_user()

    from backend.core.dependencies import get_current_user, get_db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            resp = await ac.get(
                "/api/leagues/connect/espn/callback"
                "?espn_s2=test_cookie&swid=test_swid"
            )
        assert resp.status_code == 404, (
            "the credentials-in-query-string callback is back — it must not be"
        )
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
    # Must be what production actually raises. This previously constructed a bare
    # AppError (status 500), so the test asserted its own fixture rather than the
    # behaviour — and the permissive `in (400,401,422,500)` assertion below hid it.
    mock_api.validate_cookies.side_effect = PlatformAuthError(
        "Your ESPN session expired. Reconnect ESPN to continue.",
        {"platform": "espn", "action": "reconnect"},
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
                    "/api/leagues/connect/espn",
                    json={
                        "league_id": "12345",
                        "espn_s2": "bad_cookie",
                        "swid": "bad_swid",
                    },
                )
            # This used to accept `in (400, 401, 422, 500)` — a set that INCLUDES
            # the failure mode, so the assertion was structurally incapable of
            # failing. Bad cookies are the user's problem to fix, so this must be
            # a 4xx carrying an actionable message, never an opaque 500.
            assert resp.status_code < 500, (
                f"invalid ESPN cookies surfaced as {resp.status_code} — a server "
                "error tells the user nothing they can act on"
            )
            assert resp.status_code >= 400
            # And the reason must survive to the client; the frontend renders it.
            assert "reconnect" in resp.text.lower()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_connect_detects_snake_draft():
    """ESPN connect should detect snake draft instead of hardcoding auction."""
    user = _make_user()

    from backend.core.dependencies import get_current_user, get_db
    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_api = AsyncMock()
    mock_api.validate_cookies.return_value = True
    mock_api.detect_draft_type.return_value = ("snake", None)

    mock_league = _make_league(user.id, platform="espn")
    mock_league.draft_type = "snake"

    with patch(
        "backend.integrations.espn_league_api.ESPNLeagueAPI",
        return_value=mock_api,
    ), patch(
        # league_connect imports CredentialRepository at MODULE level, so patching
        # it in its defining module does nothing — the router resolves the real
        # class and Fernet + two session.execute calls actually ran against the
        # mock session (visible as a "coroutine was never awaited" RuntimeWarning).
        # The sibling extension test already patches the right target.
        "backend.routers.league_connect.CredentialRepository",
    ) as MockCredRepo, patch(
        "backend.routers.league_connect.LeagueRepository",
    ) as MockLeagueRepo, patch(
        "backend.services.feature_service.FeatureService",
    ), patch(
        "backend.services.league_sync.LeagueSyncService",
    ) as MockSync:
        MockCredRepo.return_value.upsert_espn = AsyncMock()
        MockLeagueRepo.return_value.count_active = AsyncMock(return_value=0)

        # ESPN connect now goes through LeagueRepository.upsert (idempotent),
        # NOT LeagueService.add_league (a bare INSERT that made reconnecting
        # impossible). find_by_identity returning None = a genuinely new league,
        # so the tier check still runs.
        MockLeagueRepo.return_value.find_by_identity = AsyncMock(return_value=None)
        MockLeagueRepo.return_value.upsert = AsyncMock(return_value=mock_league)
        MockSync.return_value.sync_league = AsyncMock(
            return_value={"picks_imported": 0, "seasons_imported": 0,
                          "managers_found": 0, "free_agents_cached": 0}
        )

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as ac:
                resp = await ac.post(
                    "/api/leagues/connect/espn",
                    json={
                        "league_id": "999",
                        "espn_s2": "cookie",
                        "swid": "{SWID}",
                    },
                )
            assert resp.status_code == 200
            call_kwargs = MockLeagueRepo.return_value.upsert.call_args[1]
            assert call_kwargs["draft_type"] == "snake"
            assert call_kwargs["budget"] == 200  # fallback
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
                    f"/api/leagues/{league.id}/status"
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["platform"] == league.platform
            assert data["is_active"] is True
            assert data["last_synced"] is not None
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_league_hard_deletes_row():
    """DELETE /leagues/{id} returns 200 with status=deleted."""
    user = _make_user()
    league = _make_league(user.id)

    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    # _get_user_league calls repo.get_user_league
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = league
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.delete = AsyncMock()
    mock_db.commit = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.delete(f"/api/leagues/{league.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        # Verify db.delete was called with the league
        mock_db.delete.assert_awaited_once_with(league)
        mock_db.commit.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_cascades_auction_history():
    """DELETE /leagues/{id} deletes auction history before the league row."""
    user = _make_user()
    league = _make_league(user.id)

    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = league
    executed_stmts = []

    async def capture_execute(stmt, *a, **kw):
        executed_stmts.append(str(stmt))
        return mock_result

    mock_db.execute = capture_execute
    mock_db.delete = AsyncMock()
    mock_db.commit = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.delete(f"/api/leagues/{league.id}")
        assert resp.status_code == 200
        # At least one DELETE statement for auction history
        delete_stmts = [s for s in executed_stmts if "DELETE" in s.upper()]
        assert len(delete_stmts) >= 1, "Should DELETE auction history rows"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_requires_ownership():
    """DELETE /leagues/{id} returns 404 for non-existent league."""
    user = _make_user()
    fake_id = uuid.uuid4()

    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None  # not found
    mock_db.execute = AsyncMock(return_value=mock_result)

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.delete(f"/api/leagues/{fake_id}")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_finished_league_still_deletable():
    """DELETE works on is_active=False leagues too."""
    user = _make_user()
    league = _make_league(user.id)
    league.is_active = False  # finished season

    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = league
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.delete = AsyncMock()
    mock_db.commit = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            resp = await ac.delete(f"/api/leagues/{league.id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# ESPN connect FROM THE EXTENSION (X-Draft-Token auth) — PR 1 of 2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_espn_extension_connect_valid_token_converges_with_manual():
    """Valid draft token + valid cookies → same end state as the manual JWT path:
    validate → credential upsert → league UPSERT (detected draft type) → sync."""
    user = _make_user()
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    mock_api = AsyncMock()
    mock_api.validate_cookies.return_value = True
    mock_api.detect_draft_type.return_value = ("snake", None)
    mock_league = _make_league(user.id, platform="espn")

    user_repo = MagicMock()
    user_repo.get_by_draft_token = AsyncMock(return_value=user)

    with patch("backend.repositories.user_repo.UserRepository", return_value=user_repo), patch(
        "backend.integrations.espn_league_api.ESPNLeagueAPI", return_value=mock_api
    ), patch(
        "backend.routers.league_connect.CredentialRepository"
    ) as MockCredRepo, patch(
        "backend.routers.league_connect.LeagueRepository"
    ) as MockLeagueRepo, patch("backend.services.feature_service.FeatureService"), patch(
        "backend.services.league_sync.LeagueSyncService"
    ) as MockSync:
        MockCredRepo.return_value.upsert_espn = AsyncMock()
        MockLeagueRepo.return_value.count_active = AsyncMock(return_value=0)
        # Both ESPN endpoints share _espn_persist_and_sync, which now upserts.
        MockLeagueRepo.return_value.find_by_identity = AsyncMock(return_value=None)
        MockLeagueRepo.return_value.upsert = AsyncMock(return_value=mock_league)
        MockSync.return_value.sync_league = AsyncMock(
            return_value={"picks_imported": 3, "seasons_imported": 1,
                          "managers_found": 12, "free_agents_cached": 0}
        )
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/leagues/connect/espn/extension",
                    json={"league_id": "999", "espn_s2": "cookieval", "swid": "{SWID}"},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "connected"
            assert body["picks_imported"] == 3
            # a successful sync must say so — the wizard branches on this
            assert body["sync_failed"] is False
            # resolved via the draft token (NOT get_current_user)
            user_repo.get_by_draft_token.assert_awaited_once_with("valid-token")
            # same end state: credential upserted + league upserted with detected type
            MockCredRepo.return_value.upsert_espn.assert_awaited_once()
            assert MockLeagueRepo.return_value.upsert.call_args[1]["draft_type"] == "snake"
            # cookie values never echoed back
            assert "cookieval" not in resp.text
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_extension_connect_invalid_token_401():
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    user_repo = MagicMock()
    user_repo.get_by_draft_token = AsyncMock(return_value=None)
    with patch("backend.repositories.user_repo.UserRepository", return_value=user_repo):
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/leagues/connect/espn/extension",
                    json={"league_id": "999", "espn_s2": "x", "swid": "{SWID}"},
                    headers={"X-Draft-Token": "bad-token"},
                )
            assert resp.status_code == 401
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_extension_connect_bad_cookies_422_no_secret_echo():
    user = _make_user()
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    user_repo = MagicMock()
    user_repo.get_by_draft_token = AsyncMock(return_value=user)
    mock_api = AsyncMock()
    mock_api.validate_cookies.side_effect = RuntimeError("ESPN 401")
    with patch("backend.repositories.user_repo.UserRepository", return_value=user_repo), patch(
        "backend.integrations.espn_league_api.ESPNLeagueAPI", return_value=mock_api
    ):
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/leagues/connect/espn/extension",
                    json={"league_id": "999", "espn_s2": "supersecret", "swid": "{SWID}"},
                    headers={"X-Draft-Token": "valid-token"},
                )
            assert resp.status_code == 422
            assert "supersecret" not in resp.text  # never echo cookie values
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_extension_connect_requires_draft_token_header():
    # No X-Draft-Token → 422 (missing required header), not a silent success.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/leagues/connect/espn/extension",
            json={"league_id": "999", "espn_s2": "x", "swid": "{SWID}"},
        )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# PATCH /leagues/{id}/my-team — manual team selection (bind-failure recovery)
# ---------------------------------------------------------------------------

async def _patch_my_team(user, league, team_id):
    from backend.core.dependencies import get_current_user, get_db
    mock_db = AsyncMock()
    repo = MagicMock()
    repo.get_user_league = AsyncMock(return_value=league)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        with patch("backend.routers.league_connect.LeagueRepository", return_value=repo):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                return await ac.patch(f"/api/leagues/{league.id}/my-team", json={"team_id": team_id})
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_set_my_team_writes_manual_origin():
    """A valid pick writes my_team_id via the CANONICAL column with source='manual'."""
    user = _make_user()
    league = _make_league(user.id, platform="sleeper")
    league.manager_map = {"2": "Alice", "5": "Bob"}
    league.my_team_id = None
    league.my_team_id_source = None

    resp = await _patch_my_team(user, league, "5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["my_team_id"] == "5"
    assert body["my_team_id_source"] == "manual"
    # written to the SAME column the auto-binder uses (one identity write path)
    assert league.my_team_id == "5"
    assert league.my_team_id_source == "manual"


@pytest.mark.asyncio
async def test_set_my_team_rejects_team_not_in_league():
    """The pick is validated against the league's real teams — never a free-form guess."""
    user = _make_user()
    league = _make_league(user.id, platform="sleeper")
    league.manager_map = {"2": "Alice", "5": "Bob"}

    resp = await _patch_my_team(user, league, "99")
    assert resp.status_code == 422
    assert league.my_team_id != "99"
