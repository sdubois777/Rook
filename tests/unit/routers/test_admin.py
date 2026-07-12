"""Tests for backend/routers/admin.py"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.config import settings
from backend.core.dependencies import get_current_user
from backend.core.exceptions import UnauthorizedError
from backend.main import app
from backend.middleware.rate_limit import rate_limit_pipeline

_ADMIN_EMAIL = "admin@test.local"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _as_admin(monkeypatch):
    """Authenticate every test as an admin and skip the pipeline rate limit — the
    dedicated auth/rate-limit tests below re-override these to exercise rejection."""
    monkeypatch.setattr(settings, "admin_emails", _ADMIN_EMAIL, raising=False)
    admin = MagicMock()
    admin.email = _ADMIN_EMAIL
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[rate_limit_pipeline] = lambda: None
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(rate_limit_pipeline, None)


# ---------------------------------------------------------------------------
# Admin auth gate — reject BEFORE any pipeline stage / LLM call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_run_unauthenticated_rejected_before_spend(monkeypatch):
    """No auth → 401 and the agent runner is never invoked."""
    import backend.agents.team_systems as ts
    calls = {"n": 0}

    async def _spy(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(ts, "run_all_teams", _spy, raising=False)

    async def _raise():
        raise UnauthorizedError("no token")

    app.dependency_overrides[get_current_user] = _raise
    async with _client() as ac:
        resp = await ac.post("/api/admin/pipeline/run", json={"agent_name": "team_systems"})

    assert resp.status_code == 401
    assert calls["n"] == 0  # rejected before the pipeline stage started


@pytest.mark.asyncio
async def test_pipeline_run_non_admin_rejected_before_spend(monkeypatch):
    """Authenticated but NOT an admin → 403 and the agent runner is never invoked."""
    import backend.agents.team_systems as ts
    calls = {"n": 0}

    async def _spy(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(ts, "run_all_teams", _spy, raising=False)

    regular = MagicMock()
    regular.email = "regular-user@example.com"  # not in ADMIN_EMAILS
    app.dependency_overrides[get_current_user] = lambda: regular
    async with _client() as ac:
        resp = await ac.post("/api/admin/pipeline/run", json={"agent_name": "team_systems"})

    assert resp.status_code == 403
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_admin_reads_require_admin(monkeypatch):
    """Even a read (cost-report) rejects a non-admin — operator data isn't public."""
    regular = MagicMock()
    regular.email = "regular-user@example.com"
    app.dependency_overrides[get_current_user] = lambda: regular
    async with _client() as ac:
        resp = await ac.get("/api/admin/cost-report")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_pipeline_run_rate_limited(monkeypatch):
    """Repeated admin triggers hit the per-IP pipeline cap → 429 (anti-footgun)."""
    import backend.agents.team_systems as ts
    from backend.middleware import rate_limit as rl

    monkeypatch.setattr(ts, "run_all_teams", AsyncMock(), raising=False)
    monkeypatch.setattr(rl, "_pipeline_limiter", rl.RateLimiter(requests_per_minute=2))
    # Let the REAL rate limiter run (autouse no-op removed).
    app.dependency_overrides.pop(rate_limit_pipeline, None)

    async with _client() as ac:
        codes = [
            (await ac.post("/api/admin/pipeline/run", json={"agent_name": "team_systems"})).status_code
            for _ in range(3)
        ]

    assert codes[0] == 200 and codes[1] == 200
    assert codes[2] == 429  # third trigger within the window is capped


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
            resp = await ac.get("/api/admin/pipeline-status")

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
            "/api/admin/pipeline/run",
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
            "/api/admin/pipeline/run",
            json={"agent_name": "roster_changes", "team_abbr": "KC"},
        )

    assert resp.status_code == 200
    assert "KC" in resp.json()["message"]


@pytest.mark.asyncio
async def test_trigger_pipeline_run_invalid_agent():
    """POST /admin/pipeline/run rejects unknown agent."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/admin/pipeline/run",
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
            resp = await ac.get("/api/admin/cost-report")

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
            resp = await ac.get("/api/admin/cost-report?days=7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["period_days"] == 7
    assert data["grand_total_usd"] == 0.0
