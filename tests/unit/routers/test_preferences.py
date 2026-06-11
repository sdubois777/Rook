"""Tests for backend/routers/preferences.py"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.dependencies import get_current_user, get_db
from backend.main import app

_USER_ID = uuid.uuid4()


def _mock_user():
    m = MagicMock()
    m.id = _USER_ID
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


def _make_pref(ptype="watchlist", entity_id="player-1", value=None):
    p = MagicMock()
    p.id = uuid.uuid4()
    p.preference_type = ptype
    p.entity_id = entity_id
    p.value = value or {}
    p.created_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    p.updated_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return p


async def _request(session, method, url, **kwargs):
    """Issue one request with user + db overrides installed."""
    app.dependency_overrides[get_current_user] = _mock_user
    app.dependency_overrides[get_db] = _override_db(session)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            return await ac.request(method, url, **kwargs)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Watchlist tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_watchlist():
    """GET /preferences/watchlist returns items."""
    pref = _make_pref()
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [pref]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)

    resp = await _request(session, "GET", "/preferences/watchlist")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["player_id"] == "player-1"


@pytest.mark.asyncio
async def test_add_to_watchlist():
    """POST /preferences/watchlist adds player."""
    session = AsyncMock()
    # First execute: check existing — returns None
    result_existing = MagicMock()
    result_existing.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_existing)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock(
        side_effect=lambda p: setattr(p, 'created_at', datetime(2026, 5, 1, tzinfo=timezone.utc))
    )

    resp = await _request(
        session, "POST", "/preferences/watchlist", json={"player_id": "player-2"}
    )

    assert resp.status_code == 201
    data = resp.json()
    assert data["player_id"] == "player-2"


@pytest.mark.asyncio
async def test_add_to_watchlist_duplicate():
    """POST /preferences/watchlist returns 409 if already exists."""
    existing = _make_pref()
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=result_mock)

    resp = await _request(
        session, "POST", "/preferences/watchlist", json={"player_id": "player-1"}
    )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_remove_from_watchlist():
    """DELETE /preferences/watchlist/{id} removes player."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.rowcount = 1
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    resp = await _request(session, "DELETE", "/preferences/watchlist/player-1")

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_remove_from_watchlist_not_found():
    """DELETE /preferences/watchlist/{id} returns 404 if not in list."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.rowcount = 0
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    resp = await _request(session, "DELETE", "/preferences/watchlist/nonexistent")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Strategy tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_strategy():
    """GET /preferences/strategy returns current strategy."""
    pref = _make_pref(ptype="strategy", entity_id=None, value={"strategy": "hero_rb"})
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = pref
    session.execute = AsyncMock(return_value=result_mock)

    resp = await _request(session, "GET", "/preferences/strategy")

    assert resp.status_code == 200
    assert resp.json()["strategy"] == "hero_rb"


@pytest.mark.asyncio
async def test_set_strategy():
    """PUT /preferences/strategy sets strategy."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None  # no existing
    session.execute = AsyncMock(return_value=result_mock)
    session.add = MagicMock()
    session.commit = AsyncMock()

    resp = await _request(
        session, "PUT", "/preferences/strategy", json={"strategy": "zero_rb"}
    )

    assert resp.status_code == 200
    assert resp.json()["strategy"] == "zero_rb"


@pytest.mark.asyncio
async def test_set_strategy_invalid():
    """PUT /preferences/strategy rejects invalid strategy."""
    app.dependency_overrides[get_current_user] = _mock_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.put("/preferences/strategy", json={"strategy": "yolo"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 400
