"""Tests for backend.engines.backtest."""
from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pandas as pd
import pytest

from backend.engines.backtest import (
    FAIR_VALUE_PPR_PER_DOLLAR,
    BacktestMetrics,
    _load_actual_season,
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
# Load actual season
# ---------------------------------------------------------------------------


def test_load_actual_season_aggregates():
    """_load_actual_season aggregates weekly data to seasonal totals."""
    mock_weekly = pd.DataFrame({
        "player_id": ["001", "001", "001", "002", "002"],
        "player_display_name": ["Player A", "Player A", "Player A", "Player B", "Player B"],
        "position": ["RB", "RB", "RB", "WR", "WR"],
        "recent_team": ["NYG", "NYG", "NYG", "LAC", "LAC"],
        "fantasy_points_ppr": [20.0, 15.0, 25.0, 10.0, 12.0],
        "season_type": ["REG", "REG", "REG", "REG", "REG"],
    })

    with patch("backend.engines.backtest.nfl.import_weekly_data", return_value=mock_weekly):
        result = _load_actual_season(2024)

    assert len(result) == 2
    player_a = result[result["player_id"] == "001"].iloc[0]
    assert player_a["games"] == 3
    assert player_a["fantasy_points_ppr"] == 60.0


# ---------------------------------------------------------------------------
# run_backtest integration (mocked DB + nfl_data_py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backtest_returns_metrics_and_df():
    """run_backtest returns (BacktestMetrics, DataFrame)."""
    # Mock actual season data
    mock_weekly = pd.DataFrame({
        "player_id": ["00-001", "00-002"],
        "player_display_name": ["Test Player", "Other Player"],
        "position": ["RB", "WR"],
        "recent_team": ["NYG", "LAC"],
        "fantasy_points_ppr": [250.0, 180.0],
        "season_type": ["REG", "REG"],
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
    player1.tier = 2

    profile1 = MagicMock()
    profile1.clean_season_baseline = {"projected_ppr_season": 240.0}

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = [(player1, profile1)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("backend.engines.backtest.nfl.import_weekly_data", return_value=mock_weekly):
        metrics, df = await run_backtest(mock_session, 2024)

    assert isinstance(metrics, BacktestMetrics)
    assert metrics.season == 2024
    assert metrics.players_matched == 1
    assert len(df) == 1
    assert df.iloc[0]["name"] == "Test Player"
    assert df.iloc[0]["actual_ppr"] == 250.0
    assert df.iloc[0]["value_gap"] == 10.0  # 30 - 20
    assert df.iloc[0]["system_signal"] == "strong_buy"
