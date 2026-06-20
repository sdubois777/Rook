"""Tests for backend/routers/teams.py"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.dependencies import get_db
from backend.main import app


def _override_db(session):
    """Return a get_db override that yields the given mock session."""
    async def _get_db():
        yield session
    return _get_db


async def _request(session, url):
    """Issue one GET with the db override installed."""
    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return await ac.get(url)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def mock_team_system():
    ts = MagicMock()
    ts.team_abbr = "KC"
    ts.season_year = 2025
    ts.system_grade = "A"
    ts.system_ceiling = "A+"
    ts.notes = "Elite offense"
    ts.qb_name = "Patrick Mahomes"
    ts.qb_tier = "Elite"
    ts.qb_experience_years = 8
    ts.qb_cpoe = 3.2
    ts.qb_air_yards_per_attempt = 8.5
    ts.qb_pressure_performance = "A"
    ts.qb_downfield_aggressiveness = "High"
    ts.rookie_qb_flag = False
    ts.compound_risk_flag = False
    ts.pass_protection_grade = "A-"
    ts.run_blocking_grade = "B+"
    ts.oc_name = "Matt Nagy"
    ts.oc_scheme = "West Coast"
    ts.oc_run_pass_split_tendency = 0.58
    ts.personnel_tendency = "11 personnel heavy"
    ts.red_zone_philosophy = "Pass-first"
    return ts


@pytest.mark.asyncio
async def test_list_teams(mock_team_system):
    """GET /teams returns team list with system grades."""
    session = AsyncMock()

    # First execute: team systems
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [mock_team_system]
    result1 = MagicMock()
    result1.scalars.return_value = scalars_mock

    # Second execute: player counts
    result2 = MagicMock()
    result2.all.return_value = [("KC", 12)]

    session.execute = AsyncMock(side_effect=[result1, result2])

    resp = await _request(session, "/api/teams")

    assert resp.status_code == 200
    data = resp.json()
    assert "teams" in data
    assert len(data["teams"]) == 1
    assert data["teams"][0]["team_abbr"] == "KC"
    assert data["teams"][0]["system_grade"] == "A"


@pytest.mark.asyncio
async def test_get_team_detail(mock_team_system):
    """GET /teams/KC returns team detail."""
    session = AsyncMock()

    # First execute: team system
    result1 = MagicMock()
    result1.scalar_one_or_none.return_value = mock_team_system

    # Second execute: players
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result2 = MagicMock()
    result2.scalars.return_value = scalars_mock

    session.execute = AsyncMock(side_effect=[result1, result2])

    resp = await _request(session, "/api/teams/kc")

    assert resp.status_code == 200
    data = resp.json()
    assert data["team_abbr"] == "KC"
    assert data["qb_name"] == "Patrick Mahomes"


@pytest.mark.asyncio
async def test_get_team_not_found():
    """GET /teams/XXX returns 404."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)

    resp = await _request(session, "/api/teams/XXX")

    assert resp.status_code == 404
