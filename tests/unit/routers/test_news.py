"""Tests for backend/routers/news.py"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
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
def mock_signal():
    sig = MagicMock()
    sig.id = uuid.uuid4()
    sig.signal_type = "injury_update"
    sig.source = "ESPN"
    sig.raw_text = "Mahomes limited in practice"
    sig.confidence = "high"
    sig.flagged_at = datetime(2026, 5, 1, tzinfo=timezone.utc)
    sig.player_id = uuid.uuid4()
    return sig


@pytest.mark.asyncio
async def test_get_news_feed(mock_signal):
    """GET /news returns paginated signal feed."""
    session = AsyncMock()

    # Count query
    count_result = MagicMock()
    count_result.scalar.return_value = 1

    # Data query — returns Row-like tuples
    row = (mock_signal, "Patrick Mahomes", "KC", "QB")
    data_result = MagicMock()
    data_result.all.return_value = [row]

    session.execute = AsyncMock(side_effect=[count_result, data_result])

    resp = await _request(session, "/news")

    assert resp.status_code == 200
    data = resp.json()
    assert "signals" in data
    assert data["total"] == 1
    assert data["signals"][0]["signal_type"] == "injury_update"
    assert data["signals"][0]["player_name"] == "Patrick Mahomes"


@pytest.mark.asyncio
async def test_get_news_with_filters(mock_signal):
    """GET /news?team=KC&signal_type=injury_update filters correctly."""
    session = AsyncMock()

    count_result = MagicMock()
    count_result.scalar.return_value = 1

    row = (mock_signal, "Patrick Mahomes", "KC", "QB")
    data_result = MagicMock()
    data_result.all.return_value = [row]

    session.execute = AsyncMock(side_effect=[count_result, data_result])

    resp = await _request(session, "/news?team=KC&signal_type=injury_update&days=7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_get_news_empty():
    """GET /news returns empty when no signals."""
    session = AsyncMock()

    count_result = MagicMock()
    count_result.scalar.return_value = 0

    data_result = MagicMock()
    data_result.all.return_value = []

    session.execute = AsyncMock(side_effect=[count_result, data_result])

    resp = await _request(session, "/news")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["signals"] == []
    assert data["pages"] == 1
