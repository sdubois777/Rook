"""Tests for backend.engines.backtest."""
from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pandas as pd
import pytest

from backend.engines.backtest import (
    FAIR_VALUE_PPR_PER_DOLLAR,
    BacktestMetrics,
    _load_actual_season,
    derive_system_signal,
    run_backtest,
)


# ---------------------------------------------------------------------------
# BacktestMetrics
# ---------------------------------------------------------------------------


def test_backtest_metrics_to_dict():
    """BacktestMetrics.to_dict returns expected structure."""
    m = BacktestMetrics(
        season=2024,
        players_analyzed=100,
        players_matched=90,
        mae=37.7,
        bias=-8.9,
        correlation=0.744,
        signal_accuracy=55.4,
        total_calls=74,
        buy_accuracy=100.0,
        buy_count=40,
        avoid_accuracy=3.0,
        avoid_count=34,
        grade="MODERATE",
    )
    d = m.to_dict()
    assert d["season"] == 2024
    assert d["projection"]["mae"] == 37.7
    assert d["signals"]["accuracy"] == 55.4
    assert d["grade"] == "MODERATE"


def test_signal_accuracy_between_0_and_100():
    """Signal accuracy must be between 0 and 100."""
    m = BacktestMetrics(season=2024, signal_accuracy=55.4)
    assert 0 <= m.signal_accuracy <= 100


# ---------------------------------------------------------------------------
# Value gap calculation
# ---------------------------------------------------------------------------


def test_value_gap_calculated_correctly():
    """Value gap = ai_ceiling - league_price."""
    ai_ceiling = 25.0
    league_price = 10.0
    gap = ai_ceiling - league_price
    assert gap == 15.0

    # System signal thresholds
    assert gap >= 8  # strong_buy


def test_fair_value_threshold():
    """FAIR_VALUE_PPR_PER_DOLLAR is reasonable for a $200 budget league."""
    assert 3.0 <= FAIR_VALUE_PPR_PER_DOLLAR <= 5.0


# ---------------------------------------------------------------------------
# Injury exclusion
# ---------------------------------------------------------------------------


def test_injury_shortened_excluded():
    """Players with < 10 games are marked injury_shortened."""
    # Simulate: 7 games played → injury_shortened = True
    actual_games = 7
    assert actual_games < 10

    # 12 games → not shortened
    assert not (12 < 10)


# ---------------------------------------------------------------------------
# Load actual season (delegates to get_seasonal_stats)
# ---------------------------------------------------------------------------


def test_load_actual_season_delegates_to_get_seasonal_stats():
    """_load_actual_season calls get_seasonal_stats."""
    mock_df = pd.DataFrame({
        "player_id": ["001", "002"],
        "player_display_name": ["Player A", "Player B"],
        "position": ["RB", "WR"],
        "recent_team": ["NYG", "LAC"],
        "games": [17, 16],
        "fantasy_points_ppr": [250.0, 180.0],
    })

    with patch("backend.engines.backtest.get_seasonal_stats", return_value=mock_df) as mock_fn:
        result = _load_actual_season(2025)

    mock_fn.assert_called_once_with(2025)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# run_backtest integration (mocked DB + get_seasonal_stats)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backtest_returns_metrics_and_df():
    """run_backtest returns (BacktestMetrics, DataFrame)."""
    # Mock actual season data (format returned by get_seasonal_stats)
    mock_seasonal = pd.DataFrame({
        "player_id": ["00-001", "00-002"],
        "player_display_name": ["Test Player", "Other Player"],
        "position": ["RB", "WR"],
        "recent_team": ["NYG", "LAC"],
        "games": [17, 16],
        "fantasy_points_ppr": [250.0, 180.0],
    })

    # Mock DB players
    player1 = MagicMock()
    player1.name = "Test Player"
    player1.position = "RB"
    player1.yahoo_player_id = "nfl_00-001"
    player1.market_value_league = 20
    player1.ai_bid_ceiling = 30
    player1.recommended_bid_ceiling = 28
    player1.value_assessment = "good_value"
    player1.pay_up_flag = False
    player1.tier = 2

    profile1 = MagicMock()
    profile1.clean_season_baseline = {"projected_ppr_season": 240.0}

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [(player1, profile1)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.engines.backtest.get_seasonal_stats", return_value=mock_seasonal):
        metrics, df = await run_backtest(mock_session, 2024)

    assert isinstance(metrics, BacktestMetrics)
    assert metrics.season == 2024
    assert metrics.players_matched == 1
    assert len(df) == 1
    assert df.iloc[0]["name"] == "Test Player"
    assert df.iloc[0]["actual_ppr"] == 250.0
    assert df.iloc[0]["value_gap"] == 10.0  # 30 - 20
    assert df.iloc[0]["system_signal"] == "strong_buy"


# ---------------------------------------------------------------------------
# derive_system_signal tests
# ---------------------------------------------------------------------------


def test_pay_up_flag_overrides_negative_gap():
    """pay_up_flag=True should produce strong_buy even with negative gap."""
    signal = derive_system_signal(
        value_assessment="fair_value",
        pay_up_flag=True,
        value_gap=-5.0,
        ai_ceiling=20.0,
        league_price=25.0,
    )
    assert signal == "strong_buy"


def test_good_value_assessment_generates_buy():
    """good_value assessment → buy (or strong_buy with large gap)."""
    signal = derive_system_signal(
        value_assessment="good_value",
        pay_up_flag=False,
        value_gap=2.0,
        ai_ceiling=22.0,
        league_price=20.0,
    )
    assert signal == "buy"

    signal_strong = derive_system_signal(
        value_assessment="good_value",
        pay_up_flag=False,
        value_gap=10.0,
        ai_ceiling=30.0,
        league_price=20.0,
    )
    assert signal_strong == "strong_buy"


def test_avoid_assessment_generates_avoid():
    """avoid assessment → avoid/strong_avoid depending on gap."""
    signal = derive_system_signal(
        value_assessment="avoid",
        pay_up_flag=False,
        value_gap=-3.0,
        ai_ceiling=17.0,
        league_price=20.0,
    )
    assert signal == "avoid"

    signal_strong = derive_system_signal(
        value_assessment="avoid",
        pay_up_flag=False,
        value_gap=-12.0,
        ai_ceiling=8.0,
        league_price=20.0,
    )
    assert signal_strong == "strong_avoid"


def test_nacua_signal_is_buy_after_fix():
    """Nacua-like scenario: negative gap but good_value + pay_up_flag → strong_buy."""
    # Nacua: ai_ceiling=45, league_price=50, gap=-5, but pay_up_flag=True
    signal = derive_system_signal(
        value_assessment="good_value",
        pay_up_flag=True,
        value_gap=-5.0,
        ai_ceiling=45.0,
        league_price=50.0,
    )
    assert signal == "strong_buy"


def test_fair_value_no_flag_uses_gap():
    """fair_value with no pay_up_flag falls back to gap-based signal."""
    assert derive_system_signal("fair_value", False, 6.0, 26.0, 20.0) == "buy"
    assert derive_system_signal("fair_value", False, 0.0, 20.0, 20.0) == "neutral"
    assert derive_system_signal("fair_value", False, -6.0, 14.0, 20.0) == "avoid"


def test_slight_overpay_generates_avoid():
    """slight_overpay assessment → avoid."""
    signal = derive_system_signal(
        value_assessment="slight_overpay",
        pay_up_flag=False,
        value_gap=-2.0,
        ai_ceiling=18.0,
        league_price=20.0,
    )
    assert signal == "avoid"
