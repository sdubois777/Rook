"""Tests for backend/routers/players.py"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.dependencies import get_current_user, get_db
from backend.main import app


def _mock_user():
    m = MagicMock()
    m.id = uuid.uuid4()
    m.external_id = "test-user"
    m.email = "test@test.com"
    m.tier = "intro"
    m.credits_remaining = 25
    return m


def _override_db(session):
    """Return a get_db override that yields the given mock session."""
    async def _get_db():
        yield session
    return _get_db


def _make_player(**overrides):
    """Create a fully-populated mock player ORM object."""
    p = MagicMock()
    p.id = overrides.get("id", uuid.uuid4())
    p.name = overrides.get("name", "Ja'Marr Chase")
    p.team_abbr = overrides.get("team_abbr", "CIN")
    p.position = overrides.get("position", "WR")
    p.age = overrides.get("age", 25)
    p.tier = overrides.get("tier", 1)
    p.recommended_bid_ceiling = overrides.get("recommended_bid_ceiling", 72.5)
    p.baseline_value = overrides.get("baseline_value", 68.0)
    p.ceiling_value = overrides.get("ceiling_value", 80.0)
    p.floor_value = overrides.get("floor_value", 55.0)
    p.market_value = overrides.get("market_value", 70.0)
    p.market_value_fantasypros = overrides.get("market_value_fantasypros", None)
    p.market_value_league = overrides.get("market_value_league", None)
    p.value_gap = overrides.get("value_gap", 2.5)
    p.value_gap_signal = overrides.get("value_gap_signal", "market_undervalues")
    p.situation_score = overrides.get("situation_score", "A-")
    p.breakout_flag = overrides.get("breakout_flag", False)
    p.is_rookie = overrides.get("is_rookie", False)
    p.notes = overrides.get("notes", None)
    p.dependencies = overrides.get("dependencies", [])
    p.injury_profile = overrides.get("injury_profile", None)
    p.profile = overrides.get("profile", None)
    p.schedule = overrides.get("schedule", None)
    p.beat_signals = overrides.get("beat_signals", [])
    p.ai_bid_ceiling = overrides.get("ai_bid_ceiling", None)
    p.ai_confidence_floor = overrides.get("ai_confidence_floor", None)
    p.ai_confidence_ceiling = overrides.get("ai_confidence_ceiling", None)
    p.value_assessment = overrides.get("value_assessment", None)
    p.auction_note = overrides.get("auction_note", None)
    p.pay_up_flag = overrides.get("pay_up_flag", False)
    p.nomination_target_flag = overrides.get("nomination_target_flag", False)
    return p


# ---------------------------------------------------------------------------
# GET /players (list)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_players():
    """GET /players returns paginated list."""
    player = _make_player()
    session = AsyncMock()

    # list_players does 2 executes: count + data
    count_result = MagicMock()
    count_result.scalar.return_value = 1

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [player]
    data_result = MagicMock()
    data_result.scalars.return_value = scalars_mock

    session.execute = AsyncMock(side_effect=[count_result, data_result])

    app.dependency_overrides[get_current_user] = _mock_user
    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/players")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["page"] == 1
    assert len(data["players"]) == 1
    assert data["players"][0]["name"] == "Ja'Marr Chase"


# ---------------------------------------------------------------------------
# GET /players/search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_players():
    """GET /players/search returns matching players."""
    player = _make_player()
    session = AsyncMock()

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [player]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)

    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/players/search?q=chase")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Ja'Marr Chase"


@pytest.mark.asyncio
async def test_search_players_requires_query():
    """GET /players/search without q returns 422."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/players/search")

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /players/summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_player_summary():
    """GET /players/summary returns position counts."""
    session = AsyncMock()

    # First execute: grouped position/tier/count
    rows = [("WR", 1, 15), ("WR", 2, 20), ("RB", 1, 10), ("QB", 1, 8), ("TE", 1, 5)]
    grouped_result = MagicMock()
    grouped_result.all.return_value = rows

    # Second execute: total count
    total_result = MagicMock()
    total_result.scalar.return_value = 121

    session.execute = AsyncMock(side_effect=[grouped_result, total_result])

    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/players/summary")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
    assert "position_counts" in data
    assert data["total_players"] == 121
    assert data["position_counts"]["WR"]["tier1"] == 15
    assert data["position_counts"]["WR"]["tier2"] == 20


# ---------------------------------------------------------------------------
# GET /players/{id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_player_detail():
    """GET /players/{id} returns player detail with team system."""
    player = _make_player()
    player_id = str(player.id)

    # Mock team system
    ts = MagicMock()
    ts.team_abbr = "CIN"
    ts.system_grade = "B+"
    ts.qb_name = "Joe Burrow"
    ts.qb_tier = "Elite"
    ts.pass_protection_grade = "C+"
    ts.run_blocking_grade = "B-"
    ts.oc_scheme = "Spread"
    ts.rookie_qb_flag = False
    ts.compound_risk_flag = False

    session = AsyncMock()

    # First execute: player lookup
    player_result = MagicMock()
    player_result.scalar_one_or_none.return_value = player

    # Second execute: team system lookup
    ts_result = MagicMock()
    ts_result.scalar_one_or_none.return_value = ts

    session.execute = AsyncMock(side_effect=[player_result, ts_result])

    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(f"/players/{player_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Ja'Marr Chase"
    assert data["team_system"]["system_grade"] == "B+"
    assert data["team_system"]["qb_name"] == "Joe Burrow"


@pytest.mark.asyncio
async def test_get_player_not_found():
    """GET /players/{id} returns 404 for missing player."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    fake_id = str(uuid.uuid4())
    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get(f"/players/{fake_id}")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 404
