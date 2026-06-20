"""Tests for backend/routers/league.py history endpoints.

GET /league/history/seasons   — per-season pick counts and spend
GET /league/history/{season}  — full draft results for one season
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.dependencies import get_current_user, get_db
from backend.main import app


def _mock_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.external_id = "test-user"
    user.email = "test@test.com"
    return user


def _override_db(session):
    """Return a get_db override that yields the given mock session."""
    async def _get_db():
        yield session
    return _get_db


async def _request(session, url, user=None):
    """Issue one GET with auth + db overrides installed."""
    app.dependency_overrides[get_current_user] = lambda: user or _mock_user()
    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return await ac.get(url)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _summary_row(season_year, pick_count, total_spent, source):
    row = MagicMock()
    row.season_year = season_year
    row.pick_count = pick_count
    row.total_spent = total_spent
    row.source = source
    return row


def _pick_record(
    player_name="Christian McCaffrey",
    position="RB",
    price=50,
    manager_name="Stephen",
    draft_pick_number=1,
    player_id=None,
):
    rec = MagicMock()
    rec.player_name = player_name
    rec.position = position
    rec.price = price
    rec.manager_name = manager_name
    rec.draft_pick_number = draft_pick_number
    rec.player_id = player_id
    return rec


@pytest.mark.asyncio
async def test_history_seasons_with_data_returns_summaries():
    """GET /league/history/seasons returns one summary per season row."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [
        _summary_row(2025, 192, 2400, "yahoo"),
        _summary_row(2024, 180, 2380, "yahoo"),
    ]
    session.execute = AsyncMock(return_value=result)

    resp = await _request(session, f"/api/league/history/seasons?league_id={uuid.uuid4()}")

    assert resp.status_code == 200
    data = resp.json()
    assert data == [
        {"season": 2025, "pick_count": 192, "total_spent": 2400, "source": "yahoo"},
        {"season": 2024, "pick_count": 180, "total_spent": 2380, "source": "yahoo"},
    ]


@pytest.mark.asyncio
async def test_history_seasons_with_null_spend_coerces_to_zero():
    """GET /league/history/seasons converts a NULL total_spent to 0."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [_summary_row(2023, 10, None, "manual")]
    session.execute = AsyncMock(return_value=result)

    resp = await _request(session, f"/api/league/history/seasons?league_id={uuid.uuid4()}")

    assert resp.status_code == 200
    assert resp.json()[0]["total_spent"] == 0


@pytest.mark.asyncio
async def test_history_seasons_with_no_data_returns_empty_list():
    """GET /league/history/seasons returns [] when no history imported."""
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)

    resp = await _request(session, f"/api/league/history/seasons?league_id={uuid.uuid4()}")

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_history_season_with_picks_returns_draft_results():
    """GET /league/history/{season} returns picks with matched_to_db reflecting player_id."""
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = [
        _pick_record(
            player_name="Christian McCaffrey",
            position="RB",
            price=50,
            manager_name="Stephen",
            draft_pick_number=1,
            player_id=uuid.uuid4(),
        ),
        _pick_record(
            player_name="Mystery Kicker",
            position="K",
            price=1,
            manager_name="Rival",
            draft_pick_number=2,
            player_id=None,
        ),
    ]
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=result)

    resp = await _request(session, f"/api/league/history/2025?league_id={uuid.uuid4()}")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0] == {
        "player_name": "Christian McCaffrey",
        "position": "RB",
        "price": 50,
        "manager_name": "Stephen",
        "draft_pick_number": 1,
        "matched_to_db": True,
    }
    assert data[1]["matched_to_db"] is False


@pytest.mark.asyncio
async def test_history_season_with_no_records_returns_404():
    """GET /league/history/{season} returns 404 when season has no draft data."""
    session = AsyncMock()
    scalars = MagicMock()
    scalars.all.return_value = []
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=result)

    resp = await _request(session, f"/api/league/history/2019?league_id={uuid.uuid4()}")

    assert resp.status_code == 404
    assert "2019" in resp.json()["detail"]
