"""Tests for backend/routers/admin.py"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


# ---------------------------------------------------------------------------
# Pipeline status tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_pipeline_status():
    """GET /admin/pipeline-status returns agent freshness."""
    session = AsyncMock()

    # Each agent returns (last_run, count) — 6 agents total
    # Use a recent date so stale=False (within 7-day threshold)
    last_run = datetime.now(timezone.utc)
    result_mock = MagicMock()
    result_mock.one.return_value = (last_run, 32)

    session.execute = AsyncMock(return_value=result_mock)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.admin.AsyncSessionLocal", return_value=ctx):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/pipeline-status")

    assert resp.status_code == 200
    data = resp.json()
    assert "agents" in data
    assert len(data["agents"]) == 6
    assert data["agents"][0]["agent_name"] == "team_systems"
    assert data["agents"][0]["entity_count"] == 32
    assert data["agents"][0]["stale"] is False


# ---------------------------------------------------------------------------
# Pipeline run tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trigger_pipeline_run():
    """POST /admin/pipeline/run accepts valid agent."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/admin/pipeline/run",
            json={"agent_name": "team_systems"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "started"
    assert "team_systems" in data["message"]


@pytest.mark.asyncio
async def test_trigger_pipeline_run_with_team():
    """POST /admin/pipeline/run with team_abbr."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/admin/pipeline/run",
            json={"agent_name": "roster_changes", "team_abbr": "KC"},
        )

    assert resp.status_code == 200
    assert "KC" in resp.json()["message"]


@pytest.mark.asyncio
async def test_trigger_pipeline_run_invalid_agent():
    """POST /admin/pipeline/run rejects unknown agent."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/admin/pipeline/run",
            json={"agent_name": "fake_agent"},
        )

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Cost report tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_cost_report():
    """GET /admin/cost-report returns cost summary."""
    session = AsyncMock()

    # Mock the grouped result
    row = MagicMock()
    row.agent_name = "team_systems"
    row.total_calls = 32
    row.cache_hits = 10
    row.total_input = 50000
    row.total_output = 15000
    row.total_cost = Decimal("0.012500")

    result_mock = MagicMock()
    result_mock.all.return_value = [row]
    session.execute = AsyncMock(return_value=result_mock)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.admin.AsyncSessionLocal", return_value=ctx):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/cost-report")

    assert resp.status_code == 200
    data = resp.json()
    assert "agents" in data
    assert data["agents"][0]["agent_name"] == "team_systems"
    assert data["agents"][0]["total_calls"] == 32
    assert data["grand_total_usd"] > 0
    assert data["period_days"] == 30


@pytest.mark.asyncio
async def test_get_cost_report_custom_days():
    """GET /admin/cost-report?days=7 uses custom period."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.all.return_value = []
    session.execute = AsyncMock(return_value=result_mock)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.admin.AsyncSessionLocal", return_value=ctx):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/admin/cost-report?days=7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["period_days"] == 7
    assert data["grand_total_usd"] == 0.0
