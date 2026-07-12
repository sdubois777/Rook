"""Auth tests for backend/routers/pipeline.py — operator-only, admin-gated.

The pipeline routes trigger agent runs / scrapes that cost real money, so the
whole router is behind admin auth and the expensive triggers are rate-limited.
These tests prove rejection happens BEFORE any agent runner is invoked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.config import settings
from backend.core.dependencies import get_current_user
from backend.core.exceptions import UnauthorizedError
from backend.main import app
from backend.middleware.rate_limit import rate_limit_pipeline

_ADMIN_EMAIL = "admin@test.local"
_ROUTE = "/api/pipeline/run-team-systems"


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _as_admin(monkeypatch):
    monkeypatch.setattr(settings, "admin_emails", _ADMIN_EMAIL, raising=False)
    admin = MagicMock()
    admin.email = _ADMIN_EMAIL
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[rate_limit_pipeline] = lambda: None
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(rate_limit_pipeline, None)


@pytest.mark.asyncio
async def test_run_team_systems_unauthenticated_rejected_before_spend(monkeypatch):
    import backend.agents.team_systems as ts
    calls = {"n": 0}

    async def _spy(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(ts, "run_all_teams", _spy, raising=False)

    async def _raise():
        raise UnauthorizedError("no token")

    app.dependency_overrides[get_current_user] = _raise
    async with _client() as ac:
        resp = await ac.post(_ROUTE)

    assert resp.status_code == 401
    assert calls["n"] == 0  # no agent run scheduled


@pytest.mark.asyncio
async def test_run_team_systems_non_admin_rejected_before_spend(monkeypatch):
    import backend.agents.team_systems as ts
    calls = {"n": 0}

    async def _spy(*a, **k):
        calls["n"] += 1

    monkeypatch.setattr(ts, "run_all_teams", _spy, raising=False)

    regular = MagicMock()
    regular.email = "regular-user@example.com"
    app.dependency_overrides[get_current_user] = lambda: regular
    async with _client() as ac:
        resp = await ac.post(_ROUTE)

    assert resp.status_code == 403
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_run_team_systems_admin_starts(monkeypatch):
    import backend.agents.team_systems as ts
    monkeypatch.setattr(ts, "run_all_teams", AsyncMock(return_value={}), raising=False)

    async with _client() as ac:
        resp = await ac.post(_ROUTE)

    assert resp.status_code == 200
    assert resp.json()["status"] == "started"


@pytest.mark.asyncio
async def test_run_team_systems_rate_limited(monkeypatch):
    import backend.agents.team_systems as ts
    from backend.middleware import rate_limit as rl

    monkeypatch.setattr(ts, "run_all_teams", AsyncMock(return_value={}), raising=False)
    monkeypatch.setattr(rl, "_pipeline_limiter", rl.RateLimiter(requests_per_minute=2))
    app.dependency_overrides.pop(rate_limit_pipeline, None)  # exercise the real limiter

    async with _client() as ac:
        codes = [(await ac.post(_ROUTE)).status_code for _ in range(3)]

    assert codes[0] == 200 and codes[1] == 200
    assert codes[2] == 429
