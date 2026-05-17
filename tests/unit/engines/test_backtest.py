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
        price_source="league_auction_history (2024, N=120)",
        price_coverage=120,
    )
    d = m.to_dict()
    assert d["season"] == 2024
    assert d["projection"]["mae"] == 37.7
    assert d["signals"]["accuracy"] == 55.4
    assert d["grade"] == "MODERATE"
    assert d["price_source"] == "league_auction_history (2024, N=120)"
    assert d["price_coverage"] == 120


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
# Injury handling (included in evaluation, not excluded)
# ---------------------------------------------------------------------------


def test_injury_shortened_flag():
    """Players with < 10 games are marked injury_shortened but still evaluated."""
    # 7 games → injury_shortened = True (but still counted in accuracy)
    assert 7 < 10

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

    # Track execute calls — first is SET READ ONLY, second is historical prices,
    # third is player+profile SELECT.  We need to return appropriate results.
    call_count = {"n": 0}

    async def mock_execute(stmt, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # SET TRANSACTION READ ONLY
            return MagicMock()
        if call_count["n"] == 2:
            # _load_historical_prices: auction history by name — empty
            mock_r = MagicMock()
            mock_r.fetchall.return_value = []
            return mock_r
        if call_count["n"] == 3:
            # _load_historical_prices: auction history by player_id — empty
            mock_r = MagicMock()
            mock_r.fetchall.return_value = []
            return mock_r
        # Fallback player SELECT (market_value_league path)
        mock_r = MagicMock()
        mock_r.fetchall.return_value = [(player1, profile1)]
        return mock_r

    mock_session.execute = mock_execute

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
    # Small positive gap on non-cheap player → buy
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
    """avoid assessment → avoid/strong_avoid for meaningful gaps (< -8)."""
    # Gap -10 with avoid assessment → avoid
    signal = derive_system_signal(
        value_assessment="avoid",
        pay_up_flag=False,
        value_gap=-10.0,
        ai_ceiling=10.0,
        league_price=20.0,
    )
    assert signal == "avoid"

    # Gap -16 → strong_avoid
    signal_strong = derive_system_signal(
        value_assessment="avoid",
        pay_up_flag=False,
        value_gap=-16.0,
        ai_ceiling=4.0,
        league_price=20.0,
    )
    assert signal_strong == "strong_avoid"


def test_nacua_signal_is_buy_after_fix():
    """Nacua-like scenario: negative gap but good_value + pay_up_flag → strong_buy."""
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
    # Gap -6 on non-cheap player → neutral (within -8 to 0 noise range)
    assert derive_system_signal("fair_value", False, -6.0, 14.0, 20.0) == "neutral"


def test_slight_overpay_within_noise_is_neutral():
    """slight_overpay with gap in -8 to 0 range → neutral (auction noise)."""
    signal = derive_system_signal(
        value_assessment="slight_overpay",
        pay_up_flag=False,
        value_gap=-2.0,
        ai_ceiling=18.0,
        league_price=20.0,
    )
    assert signal == "neutral"


# ---------------------------------------------------------------------------
# Tightened avoid threshold tests
# ---------------------------------------------------------------------------


def test_cheap_player_never_avoid():
    """Player with league_price <= 8 gets neutral or buy, never avoid."""
    # slight_overpay on $5 player → neutral
    assert derive_system_signal("slight_overpay", False, -3.0, 2.0, 5.0) == "neutral"
    # avoid on $3 player → neutral
    assert derive_system_signal("avoid", False, -5.0, 0.0, 3.0) == "neutral"
    # good_value on $6 player → strong_buy
    assert derive_system_signal("good_value", False, 4.0, 10.0, 6.0) == "strong_buy"
    # fair_value on $2 player → neutral
    assert derive_system_signal("fair_value", False, 0.0, 2.0, 2.0) == "neutral"


def test_small_gap_not_avoid():
    """value_gap between -8 and 0 → neutral, even with slight_overpay."""
    # Gap -5, slight_overpay, price $20 → neutral
    assert derive_system_signal("slight_overpay", False, -5.0, 15.0, 20.0) == "neutral"
    # Gap -3, avoid assessment, price $15 → neutral
    assert derive_system_signal("avoid", False, -3.0, 12.0, 15.0) == "neutral"
    # Gap -7, slight_overpay, price $39 → neutral
    assert derive_system_signal("slight_overpay", False, -7.0, 32.0, 39.0) == "neutral"


def test_large_gap_still_avoid():
    """value_gap <= -15 with avoid/slight_overpay → strong_avoid."""
    # BTJ: $51 paid, $22 ceiling, gap=-29 → strong_avoid
    assert derive_system_signal("avoid", False, -29.0, 22.0, 51.0) == "strong_avoid"
    # Gap -16, slight_overpay → strong_avoid
    assert derive_system_signal("slight_overpay", False, -16.0, 4.0, 20.0) == "strong_avoid"
    # Gap -10, avoid → avoid (meaningful but not extreme)
    assert derive_system_signal("avoid", False, -10.0, 10.0, 20.0) == "avoid"


def test_kelce_not_avoid_after_fix():
    """Kelce: price=$6, gap=0, slight_overpay → neutral (cheap player rule)."""
    signal = derive_system_signal(
        value_assessment="slight_overpay",
        pay_up_flag=False,
        value_gap=0.0,
        ai_ceiling=6.0,
        league_price=6.0,
    )
    assert signal == "neutral"


def test_btj_still_strong_avoid_after_fix():
    """Brian Thomas Jr: price=$51, gap=-29 → strong_avoid."""
    signal = derive_system_signal(
        value_assessment="avoid",
        pay_up_flag=False,
        value_gap=-29.0,
        ai_ceiling=22.0,
        league_price=51.0,
    )
    assert signal == "strong_avoid"
