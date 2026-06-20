"""Tests for backend/routers/draftboard.py"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.dependencies import get_current_user
from backend.main import app
from backend.routers.draftboard import _apply_strategy, DraftBoardPlayer


def _mock_user():
    m = MagicMock()
    m.id = uuid.uuid4()
    m.external_id = "test-user"
    m.email = "test@test.com"
    m.tier = "intro"
    m.credits_remaining = 25
    return m


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
    p.value_assessment = None
    p.adp_ai = None
    p.adp_fantasypros = None
    p.adp_scoring = None
    p.adp_rank = None
    p.adp_diff = None
    p.snake_flag = None
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
    p.value_assessment = None
    p.adp_ai = None
    p.adp_fantasypros = None
    p.adp_scoring = None
    p.adp_rank = None
    p.adp_diff = None
    p.snake_flag = None
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

    app.dependency_overrides[get_current_user] = _mock_user
    try:
        with patch("backend.routers.draftboard.AsyncSessionLocal", return_value=ctx):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get("/api/draftboard")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

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

    app.dependency_overrides[get_current_user] = _mock_user
    try:
        with patch("backend.routers.draftboard.AsyncSessionLocal", return_value=ctx):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get("/api/draftboard?strategy=hero_rb")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["strategy"] == "hero_rb"
    # RB tier 1 should be "primary" in hero_rb
    player = data["tiers"]["1"][0]
    assert player["strategy_highlight"] == "primary"


# ---------------------------------------------------------------------------
# Snake mode
# ---------------------------------------------------------------------------

def _snake_player(name, position, adp_rank, adp_ai, adp_fp, adp_diff, snake_flag):
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = name
    p.team_abbr = "ATL"
    p.position = position
    p.tier = 1
    p.recommended_bid_ceiling = 50.0
    p.baseline_value = 40.0
    p.market_value_fantasypros = 45.0
    p.value_gap = None
    p.value_gap_signal = None
    p.breakout_flag = False
    p.is_rookie = False
    p.value_assessment = None
    p.ai_bid_ceiling = 50
    p.pay_up_flag = False
    p.nomination_target_flag = False
    p.adp_ai = adp_ai
    p.adp_fantasypros = adp_fp
    p.adp_scoring = "ppr"
    p.adp_rank = adp_rank
    p.adp_diff = adp_diff
    p.snake_flag = snake_flag
    p.dependencies = []
    p.injury_profile = None
    p.profile = None
    p.historic_prices = []
    return p


async def _call_snake_board(players):
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = players
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    app.dependency_overrides[get_current_user] = _mock_user
    try:
        with patch("backend.routers.draftboard.AsyncSessionLocal", return_value=ctx):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.get("/api/draftboard?draft_type=snake")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    return resp


@pytest.mark.asyncio
async def test_draftboard_groups_by_round_for_snake():
    # rank 3 -> round 1; rank 14 -> round 2 (12-team)
    players = [
        _snake_player("Bijan", "RB", 3, 3.0, 1.5, -1.5, "TARGET"),
        _snake_player("R2 Guy", "WR", 14, 14.0, 20.0, 6.0, "VALUE"),
    ]
    resp = await _call_snake_board(players)
    assert resp.status_code == 200
    data = resp.json()
    assert "1" in data["tiers"] and "2" in data["tiers"]  # round 1 + round 2
    r1 = data["tiers"]["1"][0]
    assert r1["adp_rank"] == 3
    assert r1["round_num"] == 1
    assert r1["snake_flag"] == "TARGET"
    assert r1["adp_diff"] == -1.5
    assert data["tiers"]["2"][0]["round_num"] == 2


@pytest.mark.asyncio
async def test_draftboard_sorts_by_adp_rank_for_snake():
    # Players arrive pre-ordered by adp_rank (as the DB returns them); the
    # response must preserve that order across rounds, with round_num computed.
    players = [
        _snake_player("P1", "RB", 1, 1.0, 2.0, 1.0, "TARGET"),
        _snake_player("P13", "WR", 13, 13.0, 30.0, 17.0, "VALUE"),
        _snake_player("P25", "TE", 25, 25.0, 24.0, -1.0, "TARGET"),
    ]
    resp = await _call_snake_board(players)
    data = resp.json()
    rounds = {k: [p["adp_rank"] for p in v] for k, v in data["tiers"].items()}
    assert rounds["1"] == [1]
    assert rounds["2"] == [13]
    assert rounds["3"] == [25]
