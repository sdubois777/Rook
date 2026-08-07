"""Tests for backend/routers/draftboard.py"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.core.dependencies import get_current_user
from backend.main import app
from backend.routers.draftboard import (
    _apply_strategy, build_positional_ranks, price_band, DraftBoardPlayer,
)


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
    p.ai_bid_ceiling = 70          # elite band — strategy highlighting reads THIS, not tier
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
    p.injury_status = None
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
    p.ai_bid_ceiling = 42
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
    p.injury_status = None
    return p


# ---------------------------------------------------------------------------
# Strategy logic unit tests
#
# The strategies are priced-based, not tier-based: `tier` is a within-position z-score
# tier, so it says "top of his position", never "expensive". Each test below names the
# dollar figure it is asserting on, at the default $200 budget:
#   elite >= $40 · mid $15-39 · value $5-14 · cheap < $5
# ---------------------------------------------------------------------------

def _p(position, ceiling, tier=1, pid="1"):
    return DraftBoardPlayer(id=pid, name="X", position=position, tier=tier,
                            ai_bid_ceiling=ceiling)


class TestPriceBand:
    def test_bands_split_at_the_documented_dollar_edges(self):
        assert price_band(60) == "elite"
        assert price_band(40) == "elite"
        assert price_band(39) == "mid"
        assert price_band(15) == "mid"
        assert price_band(14) == "value"
        assert price_band(5) == "value"
        assert price_band(4) == "cheap"
        assert price_band(1) == "cheap"

    def test_no_price_means_no_band_not_the_cheap_band(self):
        assert price_band(None) is None

    def test_bands_scale_with_a_different_budget(self):
        # A $100 league: "elite" starts at $20, not $40.
        assert price_band(25, budget=100) == "elite"
        assert price_band(25, budget=200) == "mid"


class TestHeroRb:
    """'Pays premium for 1-2 elite RBs, cheap elsewhere.'"""

    def test_the_top_two_elite_rbs_are_the_hero_candidates(self):
        assert _apply_strategy(_p("RB", 70), "hero_rb", pos_rank=1) == "primary"
        assert _apply_strategy(_p("RB", 62), "hero_rb", pos_rank=2) == "primary"

    def test_a_third_premium_rb_is_dimmed_not_a_third_hero(self):
        assert _apply_strategy(_p("RB", 55), "hero_rb", pos_rank=3) == "dimmed"

    def test_mid_priced_rbs_are_the_dead_zone_this_strategy_skips(self):
        assert _apply_strategy(_p("RB", 25), "hero_rb", pos_rank=6) == "dimmed"

    def test_cheap_rbs_are_still_worth_filling_with(self):
        assert _apply_strategy(_p("RB", 8), "hero_rb", pos_rank=20) == "secondary"

    def test_another_elite_price_tag_competes_with_the_hero(self):
        assert _apply_strategy(_p("WR", 55), "hero_rb", pos_rank=1) == "dimmed"

    def test_cheap_elsewhere_is_the_plan(self):
        assert _apply_strategy(_p("WR", 9), "hero_rb", pos_rank=30) == "secondary"
        assert _apply_strategy(_p("TE", 3), "hero_rb", pos_rank=14) == "secondary"


class TestZeroRb:
    """'Avoids expensive RBs, invests in WR/TE/QB.'"""

    def test_expensive_wrs_and_tes_are_the_investment(self):
        assert _apply_strategy(_p("WR", 55), "zero_rb") == "primary"
        assert _apply_strategy(_p("TE", 30), "zero_rb") == "primary"
        assert _apply_strategy(_p("QB", 22), "zero_rb") == "primary"

    def test_expensive_rbs_are_avoided(self):
        assert _apply_strategy(_p("RB", 60), "zero_rb", pos_rank=1) == "dimmed"
        assert _apply_strategy(_p("RB", 22), "zero_rb", pos_rank=8) == "dimmed"

    def test_cheap_late_rbs_are_the_point_of_the_strategy_not_dimmed(self):
        # The old rule dimmed EVERY running back, including these — inverting the advice.
        assert _apply_strategy(_p("RB", 4), "zero_rb", pos_rank=30) == "secondary"
        assert _apply_strategy(_p("RB", 11), "zero_rb", pos_rank=24) == "secondary"


class TestStarsAndScrubs:
    """'Spends big on 2-3 studs, fills rest at $1.'"""

    def test_the_expensive_players_are_the_stars(self):
        assert _apply_strategy(_p("WR", 58), "stars_and_scrubs") == "primary"

    def test_minimum_price_players_are_the_scrubs(self):
        assert _apply_strategy(_p("RB", 2), "stars_and_scrubs") == "secondary"

    def test_the_middle_of_the_board_is_what_this_strategy_skips(self):
        assert _apply_strategy(_p("TE", 20), "stars_and_scrubs") == "dimmed"

    def test_a_top_of_position_player_who_is_cheap_is_not_a_star(self):
        # A tier-1 TE at $9 tops his position but is not a stud. The old tier-based rule
        # called him "primary".
        assert _apply_strategy(_p("TE", 9, tier=1), "stars_and_scrubs") is None


class TestBalanced:
    """'Spreads budget relatively evenly across positions' — and it must DO something."""

    def test_balanced_is_not_a_no_op(self):
        assert _apply_strategy(_p("RB", 25), "balanced") == "primary"
        assert _apply_strategy(_p("WR", 9), "balanced") == "secondary"
        assert _apply_strategy(_p("WR", 58), "balanced") == "dimmed"


class TestStrategyEdgeCases:
    def test_null_tier_does_not_raise(self):
        # `tier >= 4` against a NULL tier raised TypeError and failed the whole request.
        for s in ("hero_rb", "zero_rb", "stars_and_scrubs", "balanced"):
            assert _apply_strategy(_p("WR", 30, tier=None), s, pos_rank=4) in (
                "primary", "secondary", "dimmed", None,
            )

    def test_kickers_and_defenses_are_never_highlighted(self):
        for s in ("hero_rb", "zero_rb", "stars_and_scrubs", "balanced"):
            assert _apply_strategy(_p("K", 1, tier=5), s, pos_rank=1) is None
            assert _apply_strategy(_p("DEF", 1, tier=5), s, pos_rank=1) is None

    def test_an_unpriced_player_gets_no_verdict(self):
        assert _apply_strategy(_p("WR", None), "stars_and_scrubs") is None

    def test_unknown_strategy_highlights_nothing(self):
        assert _apply_strategy(_p("WR", 55), "not_a_strategy") is None


class TestPositionalRanks:
    def test_ranked_within_position_by_our_own_ceiling(self):
        board = [
            _p("RB", 70, pid="rb1"), _p("RB", 45, pid="rb2"), _p("RB", 12, pid="rb3"),
            _p("WR", 55, pid="wr1"), _p("WR", 60, pid="wr2"),
        ]
        ranks = build_positional_ranks(board)
        assert ranks["rb1"] == 1 and ranks["rb2"] == 2 and ranks["rb3"] == 3
        assert ranks["wr2"] == 1 and ranks["wr1"] == 2   # ordered by $, not by list order

    def test_unpriced_players_rank_last(self):
        board = [_p("RB", None, pid="none"), _p("RB", 10, pid="cheap")]
        ranks = build_positional_ranks(board)
        assert ranks["cheap"] == 1 and ranks["none"] == 2


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
    p.injury_status = None
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


# ---------------------------------------------------------------------------
# Per-format market $ + GAP (the two columns must agree with each other)
# ---------------------------------------------------------------------------

def _format_player(**over):
    """An auction-board player with every field the response builder reads."""
    p = MagicMock()
    p.id = over.get("id", uuid.uuid4())
    p.name = "Ja'Marr Chase"
    p.team_abbr = "CIN"
    p.position = "WR"
    p.tier = 1
    p.recommended_bid_ceiling = 60.0
    p.baseline_value = 55.0
    p.market_value_fantasypros = over.get("market_value_fantasypros", 61.0)
    p.value_gap = over.get("value_gap", 38.0)          # the stale PPR gap
    p.value_gap_signal = "market_undervalues"
    p.ai_bid_ceiling = over.get("ai_bid_ceiling", 99)  # PPR ceiling
    p.adjusted_points = None
    p.breakout_flag = False
    p.is_rookie = False
    p.value_assessment = None
    p.pay_up_flag = False
    p.nomination_target_flag = False
    p.availability_factor = 1.0
    p.availability_games_missed = 0
    p.adp_ai = None
    p.adp_fantasypros = 12.0
    p.adp_scoring = "ppr"
    p.adp_rank = 10
    p.adp_diff = 2.0
    p.snake_flag = "TARGET"
    p.dependencies = []
    p.injury_profile = None
    p.injury_status = None
    p.profile = None
    p.historic_prices = []
    return p


async def _call_board(players, query, fmt_rows=None):
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
        with patch("backend.routers.draftboard.AsyncSessionLocal", return_value=ctx), \
             patch("backend.services.format_display.load_format_rows",
                   AsyncMock(return_value=fmt_rows or {})):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                return await ac.get(f"/api/draftboard{query}")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _fmt_row(**over):
    from types import SimpleNamespace
    return SimpleNamespace(
        tier=over.get("tier", 2),
        projected_points=over.get("projected_points", 240.0),
        adp_fantasypros=over.get("adp_fantasypros", None),
        ai_bid_ceiling=over.get("ai_bid_ceiling", 44),
        value_assessment=over.get("value_assessment", None),
        auction_note=None,
        recommended_bid_ceiling=over.get("recommended_bid_ceiling", 42.0),
        baseline_value=over.get("baseline_value", 38.0),
        auction_value=over.get("auction_value", 30.0),
    )


@pytest.mark.asyncio
async def test_hero_rb_names_one_hero_over_the_whole_board():
    """The 'top two RBs' is a property of the board, so the strategy pass has to run
    after every row is built, not row-by-row."""
    rb1 = _format_player(); rb1.name = "RB1"; rb1.position = "RB"; rb1.ai_bid_ceiling = 70
    rb2 = _format_player(); rb2.name = "RB2"; rb2.position = "RB"; rb2.ai_bid_ceiling = 62
    rb3 = _format_player(); rb3.name = "RB3"; rb3.position = "RB"; rb3.ai_bid_ceiling = 48
    resp = await _call_board([rb1, rb2, rb3], "?strategy=hero_rb&scoring_format=ppr")
    assert resp.status_code == 200
    rows = {r["name"]: r["strategy_highlight"] for r in resp.json()["tiers"]["1"]}
    assert rows == {"RB1": "primary", "RB2": "primary", "RB3": "dimmed"}


@pytest.mark.asyncio
async def test_balanced_strategy_actually_highlights_something():
    """Selecting Balanced used to change nothing on the board at all."""
    p = _format_player(); p.ai_bid_ceiling = 25   # mid band
    resp = await _call_board([p], "?strategy=balanced&scoring_format=ppr")
    assert resp.json()["tiers"]["1"][0]["strategy_highlight"] == "primary"


@pytest.mark.asyncio
async def test_stars_and_scrubs_survives_a_null_tier():
    """`tier >= 4` against a NULL tier raised TypeError and 500'd the whole board."""
    p = _format_player(); p.tier = None; p.ai_bid_ceiling = 55
    resp = await _call_board([p], "?strategy=stars_and_scrubs&scoring_format=ppr")
    assert resp.status_code == 200
    assert resp.json()["tiers"]["0"][0]["strategy_highlight"] == "primary"


@pytest.mark.asyncio
async def test_ppr_board_keeps_the_players_table_market_and_gap():
    """PPR is byte-identical — no overlay, no recompute."""
    p = _format_player()
    resp = await _call_board([p], "?scoring_format=ppr")
    assert resp.status_code == 200
    row = resp.json()["tiers"]["1"][0]
    assert row["market_value"] == 61.0
    assert row["value_gap"] == 38.0
    assert resp.json()["market_format_defaulted"] is False


@pytest.mark.asyncio
async def test_non_ppr_board_shows_format_market_and_a_gap_that_matches_it():
    """The reported bug: ceiling moved with the format, market $ and GAP did not."""
    p = _format_player()
    resp = await _call_board(
        [p], "?scoring_format=standard", fmt_rows={str(p.id): _fmt_row()},
    )
    assert resp.status_code == 200
    row = resp.json()["tiers"]["2"][0]       # per-format tier
    assert row["ai_bid_ceiling"] == 44       # per-format ceiling
    assert row["market_value"] == 30.0       # per-format market $, NOT the PPR 61
    assert row["value_gap"] == 14.0          # 44 - 30, NOT the stale PPR 38
    # The invariant: what is printed in GAP is the difference of the two printed columns.
    assert row["value_gap"] == row["ai_bid_ceiling"] - row["market_value"]
    assert resp.json()["market_format_defaulted"] is False


@pytest.mark.asyncio
async def test_non_ppr_without_per_format_market_falls_back_and_discloses():
    """No per-format DraftWizard $ yet: show the PPR $ but still make GAP agree with it."""
    p = _format_player()
    resp = await _call_board(
        [p], "?scoring_format=half_ppr",
        fmt_rows={str(p.id): _fmt_row(auction_value=None)},
    )
    data = resp.json()
    row = data["tiers"]["2"][0]
    assert row["market_value"] == 61.0                  # PPR fallback
    assert row["value_gap"] == 44 - 61                  # recomputed against what is shown
    assert row["value_gap_signal"] == "market_overvalues"
    assert data["market_format_defaulted"] is True


@pytest.mark.asyncio
async def test_non_ppr_snake_diff_and_flag_follow_the_format_adp():
    """Diff is (FP rank − our rank). Once FP rank is the format's, the stored PPR diff
    is a subtraction of numbers no longer on screen."""
    p = _format_player()
    resp = await _call_board(
        [p], "?scoring_format=standard",
        fmt_rows={str(p.id): _fmt_row(adp_fantasypros=40.0)},
    )
    row = resp.json()["tiers"]["2"][0]
    assert row["adp_fantasypros"] == 40.0
    assert row["adp_diff"] == 30.0            # 40 - adp_rank 10, not the stored 2.0
    assert row["adp_diff"] == row["adp_fantasypros"] - row["adp_rank"]
    assert row["snake_flag"] == "VALUE"       # diff >= 15 and per-format tier 2


@pytest.mark.asyncio
async def test_non_ppr_snake_diff_untouched_when_format_adp_missing():
    p = _format_player()
    resp = await _call_board(
        [p], "?scoring_format=standard",
        fmt_rows={str(p.id): _fmt_row(adp_fantasypros=None)},
    )
    row = resp.json()["tiers"]["2"][0]
    assert row["adp_fantasypros"] == 12.0     # players-table PPR value
    assert row["adp_diff"] == 2.0             # stored value, correct for that ADP
    assert row["snake_flag"] == "TARGET"


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
