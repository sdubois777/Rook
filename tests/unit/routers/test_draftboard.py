"""Tests for backend/routers/draftboard.py"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.routers.draftboard import _apply_strategy, DraftBoardPlayer


@pytest.fixture
def mock_player_rb_tier1():
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = "Saquon Barkley"
    p.team_abbr = "PHI"
    p.position = "RB"
    p.tier = 1
    p.recommended_bid_ceiling = 75.0
    p.baseline_value = 70.0
    p.market_value = 72.0
    p.value_gap = 3.0
    p.value_gap_signal = "undervalued"
    p.breakout_flag = False
    p.is_rookie = False
    p.dependencies = []
    p.injury_profile = None
    return p


@pytest.fixture
def mock_player_wr_tier2():
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = "DK Metcalf"
    p.team_abbr = "SEA"
    p.position = "WR"
    p.tier = 2
    p.recommended_bid_ceiling = 45.0
    p.baseline_value = 40.0
    p.market_value = 50.0
    p.value_gap = -5.0
    p.value_gap_signal = "overvalued"
    p.breakout_flag = False
    p.is_rookie = False
    p.dependencies = []
    p.injury_profile = None
    return p


# ---------------------------------------------------------------------------
# Strategy logic unit tests
# ---------------------------------------------------------------------------

def test_hero_rb_primary():
    player = DraftBoardPlayer(id="1", name="X", position="RB", tier=1)
    assert _apply_strategy(player, "hero_rb") == "primary"


def test_hero_rb_secondary():
    player = DraftBoardPlayer(id="1", name="X", position="WR", tier=1)
    assert _apply_strategy(player, "hero_rb") == "secondary"


def test_hero_rb_none():
    player = DraftBoardPlayer(id="1", name="X", position="TE", tier=3)
    assert _apply_strategy(player, "hero_rb") is None


def test_zero_rb_primary():
    player = DraftBoardPlayer(id="1", name="X", position="WR", tier=1)
    assert _apply_strategy(player, "zero_rb") == "primary"


def test_zero_rb_dimmed():
    player = DraftBoardPlayer(id="1", name="X", position="RB", tier=1)
    assert _apply_strategy(player, "zero_rb") == "dimmed"


def test_zero_rb_te_secondary():
    player = DraftBoardPlayer(id="1", name="X", position="TE", tier=1)
    assert _apply_strategy(player, "zero_rb") == "secondary"


def test_stars_and_scrubs_primary():
    player = DraftBoardPlayer(id="1", name="X", position="WR", tier=1)
    assert _apply_strategy(player, "stars_and_scrubs") == "primary"


def test_stars_and_scrubs_secondary():
    player = DraftBoardPlayer(id="1", name="X", position="RB", tier=5)
    assert _apply_strategy(player, "stars_and_scrubs") == "secondary"


def test_balanced_no_highlight():
    player = DraftBoardPlayer(id="1", name="X", position="RB", tier=1)
    assert _apply_strategy(player, "balanced") is None


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_draftboard(mock_player_rb_tier1, mock_player_wr_tier2):
    """GET /draftboard returns tiered response."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [mock_player_rb_tier1, mock_player_wr_tier2]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.draftboard.AsyncSessionLocal", return_value=ctx):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/draftboard")

    assert resp.status_code == 200
    data = resp.json()
    assert "tiers" in data
    assert data["total_players"] == 2
    assert "1" in data["tiers"]  # tier 1
    assert "2" in data["tiers"]  # tier 2


@pytest.mark.asyncio
async def test_get_draftboard_with_strategy(mock_player_rb_tier1):
    """GET /draftboard?strategy=hero_rb applies highlighting."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [mock_player_rb_tier1]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.routers.draftboard.AsyncSessionLocal", return_value=ctx):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/draftboard?strategy=hero_rb")

    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy"] == "hero_rb"
    # RB tier 1 should be "primary" in hero_rb
    player = data["tiers"]["1"][0]
    assert player["strategy_highlight"] == "primary"
