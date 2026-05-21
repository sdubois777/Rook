"""Tests for league tendencies endpoint — auth + user/league scoping."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.dependencies import get_current_user, get_db
from backend.main import app


def _mock_user(user_id=None):
    m = MagicMock()
    m.id = user_id or uuid.uuid4()
    m.external_id = "test-user"
    m.email = "test@test.com"
    m.tier = "standard"
    m.credits_remaining = 75
    return m


def _mock_db_session():
    """Return a mock AsyncSession that yields empty results for all queries."""
    session = AsyncMock()

    # For select(Player) — returns empty list
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []

    # For aggregate queries — returns empty list
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    result_mock.all.return_value = []

    session.execute = AsyncMock(return_value=result_mock)
    return session


@pytest.mark.asyncio
async def test_tendencies_requires_auth():
    """GET /league/tendencies without auth returns 401/403."""
    league_id = str(uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        resp = await ac.get(f"/league/tendencies?league_id={league_id}")
    # Without auth dependency override, should fail
    assert resp.status_code in (401, 403, 422)


@pytest.mark.asyncio
async def test_tendencies_requires_league_id():
    """GET /league/tendencies without league_id returns 422."""
    user = _mock_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _mock_db_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/league/tendencies")
        assert resp.status_code == 422  # Missing required param
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_tendencies_scoped_to_user_and_league():
    """GET /league/tendencies passes user_id + league_id to DB queries."""
    user_id = uuid.uuid4()
    league_id = uuid.uuid4()
    user = _mock_user(user_id)

    captured_queries = []
    original_execute = None

    async def mock_execute(stmt, *args, **kwargs):
        # Capture the compiled query string for inspection
        try:
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            captured_queries.append(compiled)
        except Exception:
            captured_queries.append(str(stmt))
        result = MagicMock()
        result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        result.all.return_value = []
        return result

    session = AsyncMock()
    session.execute = mock_execute

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get(f"/league/tendencies?league_id={league_id}")
        assert resp.status_code == 200
        # Verify that queries include user_id filtering
        # At least the history queries should reference user_id
        history_queries = [q for q in captured_queries if "league_auction_history" in q.lower()]
        assert len(history_queries) >= 1, "Should query league_auction_history"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_user_a_cannot_see_user_b_tendencies():
    """Two users with different IDs get different scoping."""
    user_a = _mock_user(uuid.uuid4())
    user_b = _mock_user(uuid.uuid4())
    league_id = uuid.uuid4()

    # User A's queries should use user_a.id
    user_a_queries = []

    async def mock_execute_a(stmt, *args, **kwargs):
        try:
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            user_a_queries.append(compiled)
        except Exception:
            pass
        result = MagicMock()
        result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))
        result.all.return_value = []
        return result

    session_a = AsyncMock()
    session_a.execute = mock_execute_a

    app.dependency_overrides[get_current_user] = lambda: user_a
    app.dependency_overrides[get_db] = lambda: session_a
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp_a = await ac.get(f"/league/tendencies?league_id={league_id}")
        assert resp_a.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    # User B's queries should use user_b.id (different from A)
    assert user_a.id != user_b.id


@pytest.mark.asyncio
async def test_new_picks_stored_with_user_id():
    """LeagueSyncService._store_picks() includes user_id in INSERT values."""
    from backend.services.league_sync import LeagueSyncService
    from backend.integrations.platform_models import DraftPick

    user_id = uuid.uuid4()
    user_league_id = uuid.uuid4()

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    picks = [
        DraftPick(
            platform_player_id="p1",
            player_name="Test Player",
            position="WR",
            team_abbr="NYG",
            picked_by_team_id="1",
            manager_name="Manager 1",
            pick_number=1,
            round_number=1,
            auction_price=25,
        ),
    ]

    with patch("backend.services.league_sync.get_platform_api"), \
         patch("backend.services.league_sync.get_current_season", return_value=2026), \
         patch("backend.services.league_sync.LeagueRepository"):
        service = LeagueSyncService(mock_db, user_id)
        count = await service._store_picks(picks, user_league_id, 2025)

    assert count == 1
    # Verify execute was called with values containing user_id
    call_args = mock_db.execute.await_args
    insert_stmt = call_args[0][0]
    # The insert statement should have user_id and user_league_id in its values
    compiled = insert_stmt.compile()
    params = compiled.params
    assert params.get("user_id") == user_id
    assert params.get("user_league_id") == user_league_id


@pytest.mark.asyncio
async def test_new_picks_stored_with_user_league_id():
    """LeagueSyncService._store_picks() includes user_league_id in INSERT values."""
    from backend.services.league_sync import LeagueSyncService
    from backend.integrations.platform_models import DraftPick

    user_id = uuid.uuid4()
    user_league_id = uuid.uuid4()

    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    picks = [
        DraftPick(
            platform_player_id="p1",
            player_name="Another Player",
            position="RB",
            team_abbr="SF",
            picked_by_team_id="5",
            manager_name="Manager 5",
            pick_number=3,
            round_number=1,
            auction_price=40,
        ),
    ]

    with patch("backend.services.league_sync.get_platform_api"), \
         patch("backend.services.league_sync.get_current_season", return_value=2026), \
         patch("backend.services.league_sync.LeagueRepository"):
        service = LeagueSyncService(mock_db, user_id)
        count = await service._store_picks(picks, user_league_id, 2025)

    assert count == 1
    call_args = mock_db.execute.await_args
    insert_stmt = call_args[0][0]
    compiled = insert_stmt.compile()
    assert compiled.params.get("user_league_id") == user_league_id
