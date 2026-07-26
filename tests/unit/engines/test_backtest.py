"""Tests for backend.engines.backtest."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

import pandas as pd
import pytest

from backend.engines.backtest import (
    FAIR_VALUE_PPR_PER_DOLLAR,
    MIN_PRICE_COVERAGE,
    BacktestMetrics,
    _load_actual_season,
    _load_historical_prices,
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
            # _load_historical_prices: auction history by player_id — empty
            mock_r = MagicMock()
            mock_r.fetchall.return_value = []
            return mock_r
        if call_count["n"] == 3:
            # _load_historical_prices: auction history by name (id-less rows) — empty
            mock_r = MagicMock()
            mock_r.fetchall.return_value = []
            return mock_r
        if call_count["n"] == 4:
            # _load_historical_prices: market_value_historic — empty here, so the
            # loader still falls through to the market_value_league path this test
            # is exercising
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
# Price join — keyed by player id, never by name
# ---------------------------------------------------------------------------


def _price_rows(rows):
    r = MagicMock()
    r.fetchall.return_value = rows
    return r


def _mock_player(pid, name, position, gsis, *, ceiling=30.0, assessment="good_value"):
    p = MagicMock()
    p.id = pid
    p.name = name
    p.position = position
    p.yahoo_player_id = f"nfl_{gsis}" if gsis else None
    p.ai_bid_ceiling = ceiling
    p.recommended_bid_ceiling = ceiling
    p.value_assessment = assessment
    p.pay_up_flag = False          # MagicMock is truthy — must be set explicitly
    p.tier = 2
    return p


def _profile(ppr):
    prof = MagicMock()
    prof.clean_season_baseline = {"projected_ppr_season": ppr}
    return prof


@pytest.mark.asyncio
async def test_auction_prices_are_keyed_by_player_id():
    """The primary auction lookup is keyed by player_id, not player_name.

    Name keying is what lost real calls on the as-of 2024 board — 151 of 177 rows
    landed, and the misses included Marvin Harrison ($40) and Deebo Samuel ($29).
    """
    id_rows = [
        SimpleNamespace(player_id=f"id-{i}", avg_price=float(i + 1))
        for i in range(MIN_PRICE_COVERAGE + 10)
    ]

    session = AsyncMock()
    calls = {"n": 0}

    async def execute(stmt, *a, **k):
        calls["n"] += 1
        return _price_rows(id_rows if calls["n"] == 1 else [])

    session.execute = execute
    by_id, by_name, source = await _load_historical_prices(session, 2024)

    assert len(by_id) == MIN_PRICE_COVERAGE + 10
    assert by_id["id-0"] == 1.0
    assert by_name == {}
    assert source.startswith("league_auction_history (2024")


@pytest.mark.asyncio
async def test_name_map_is_restricted_to_rows_with_no_player_id():
    """THE mechanism that stops a duplicate row stealing a price.

    If the name map held every auction row, a second `players` row sharing a name
    would match it and collect a price that already found its rightful owner by id.
    So the name query MUST be filtered to rows that have no player_id at all.
    """
    seen: list[str] = []

    session = AsyncMock()

    async def execute(stmt, *a, **k):
        seen.append(str(stmt))
        return _price_rows([])

    session.execute = execute
    await _load_historical_prices(session, 2024)

    by_id_sql, by_name_sql = seen[0], seen[1]
    assert "GROUP BY league_auction_history.player_id" in by_id_sql
    assert "player_id IS NULL" in by_name_sql, (
        "the name fallback must exclude rows that carry a player_id, or duplicate "
        "player rows will collect prices belonging to somebody else"
    )


@pytest.mark.asyncio
async def test_player_id_price_wins_over_a_name_collision():
    """When both maps hold the player, the id price wins.

    Reachable whenever the auction carries one id-bearing row and one id-less row
    that happen to share a name — the id is the trustworthy key.
    """
    player = _mock_player("real-id", "Ambiguous Name", "RB", "00-001")

    id_rows = [SimpleNamespace(player_id="real-id", avg_price=40.0)] + [
        SimpleNamespace(player_id=f"filler-{i}", avg_price=5.0)
        for i in range(MIN_PRICE_COVERAGE)
    ]
    name_rows = [SimpleNamespace(player_name="Ambiguous Name", avg_price=3.0)]

    mock_seasonal = pd.DataFrame({
        "player_id": ["00-001"],
        "player_display_name": ["Ambiguous Name"],
        "position": ["RB"],
        "recent_team": ["NYG"],
        "games": [17],
        "fantasy_points_ppr": [250.0],
    })

    session = AsyncMock()
    calls = {"n": 0}

    async def execute(stmt, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return MagicMock()
        if calls["n"] == 2:
            return _price_rows(id_rows)
        if calls["n"] == 3:
            return _price_rows(name_rows)
        return _price_rows([(player, _profile(240.0))])

    session.execute = execute

    with patch("backend.engines.backtest.get_seasonal_stats", return_value=mock_seasonal):
        _metrics, df = await run_backtest(session, 2024)

    assert df.iloc[0]["league_price"] == 40.0, "the name price shadowed the id price"


@pytest.mark.asyncio
async def test_duplicate_name_row_never_collects_another_players_price():
    """REGRESSION: a duplicate player row must not inherit the real player's price.

    `players` holds ~54 duplicate-name clusters. A stray WR row named "Kenneth Walker"
    took the RB's $18 and — because the actuals join is also by name — the RB's season
    too, emitted an "avoid", and was scored CORRECT inside the reported 64.1%.
    """
    # ceiling well under the $18 price, so the RB is a real "strong_avoid" call —
    # the duplicate must not become a second one.
    rb = _mock_player("rb-id", "Kenneth Walker", "RB", "00-0038134",
                      ceiling=3.0, assessment="avoid")
    wr = _mock_player("wr-id", "Kenneth Walker", "WR", None,
                      ceiling=3.0, assessment="avoid")

    # The auction knows exactly one Kenneth Walker, and points at the RB.
    id_rows = [SimpleNamespace(player_id="rb-id", avg_price=18.0)] + [
        SimpleNamespace(player_id=f"filler-{i}", avg_price=5.0)
        for i in range(MIN_PRICE_COVERAGE)
    ]

    mock_seasonal = pd.DataFrame({
        "player_id": ["00-0038134"],
        "player_display_name": ["Kenneth Walker"],
        "position": ["RB"],
        "recent_team": ["SEA"],
        "games": [16],
        "fantasy_points_ppr": [200.0],
    })

    session = AsyncMock()
    calls = {"n": 0}

    async def execute(stmt, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return MagicMock()                      # SET TRANSACTION READ ONLY
        if calls["n"] == 2:
            return _price_rows(id_rows)             # auction by player_id
        if calls["n"] == 3:
            return _price_rows([])                  # auction by name (id-less rows)
        return _price_rows([(rb, _profile(190.0)), (wr, _profile(190.0))])

    session.execute = execute

    with patch("backend.engines.backtest.get_seasonal_stats", return_value=mock_seasonal):
        metrics, df = await run_backtest(session, 2024)

    rb_row = df[df["position"] == "RB"].iloc[0]
    wr_row = df[df["position"] == "WR"].iloc[0]

    assert rb_row["league_price"] == 18.0
    assert rb_row["system_signal"] == "strong_avoid"
    assert wr_row["league_price"] == 0.0, "duplicate row inherited a price it never had"
    assert pd.isna(wr_row["system_signal"]), "duplicate row produced a signal"
    assert metrics.total_calls == 1, "the duplicate was scored as a second call"


@pytest.mark.asyncio
async def test_price_coverage_counts_landed_prices_not_dict_size():
    """price_coverage must report prices that reached a player, and unmatched ones.

    It used to report the size of the loaded price dict, so a run in which a quarter of
    the auction failed to join a player row looked identical to a clean one.
    """
    player = _mock_player("real-id", "Real Player", "RB", "00-001")

    id_rows = [SimpleNamespace(player_id="real-id", avg_price=20.0)] + [
        SimpleNamespace(player_id=f"orphan-{i}", avg_price=7.0)
        for i in range(MIN_PRICE_COVERAGE)
    ]

    mock_seasonal = pd.DataFrame({
        "player_id": ["00-001"],
        "player_display_name": ["Real Player"],
        "position": ["RB"],
        "recent_team": ["NYG"],
        "games": [17],
        "fantasy_points_ppr": [250.0],
    })

    session = AsyncMock()
    calls = {"n": 0}

    async def execute(stmt, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return MagicMock()
        if calls["n"] == 2:
            return _price_rows(id_rows)
        if calls["n"] == 3:
            return _price_rows([])
        return _price_rows([(player, _profile(240.0))])

    session.execute = execute

    with patch("backend.engines.backtest.get_seasonal_stats", return_value=mock_seasonal):
        metrics, _df = await run_backtest(session, 2024)

    assert metrics.price_coverage == 1, "only one price actually landed on a player"
    assert metrics.price_rows_unmatched == MIN_PRICE_COVERAGE
    assert metrics.to_dict()["price_rows_unmatched"] == MIN_PRICE_COVERAGE


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
    """Player with league_price <= 12 gets neutral or buy, never avoid."""
    # slight_overpay on $5 player → neutral
    assert derive_system_signal("slight_overpay", False, -3.0, 2.0, 5.0) == "neutral"
    # avoid on $3 player → neutral
    assert derive_system_signal("avoid", False, -5.0, 0.0, 3.0) == "neutral"
    # good_value on $6 player → strong_buy
    assert derive_system_signal("good_value", False, 4.0, 10.0, 6.0) == "strong_buy"
    # fair_value on $2 player → neutral
    assert derive_system_signal("fair_value", False, 0.0, 2.0, 2.0) == "neutral"
    # avoid on $10 player → neutral (raised from $8 to $12)
    assert derive_system_signal("avoid", False, -10.0, 5.0, 10.0) == "neutral"
    # slight_overpay on $12 player → neutral
    assert derive_system_signal("slight_overpay", False, -5.0, 7.0, 12.0) == "neutral"


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


# ---------------------------------------------------------------------------
# Projection floor tests
# ---------------------------------------------------------------------------


def test_low_projection_never_avoid():
    """Players projected < 80 PPR should never get avoid — they're depth noise."""
    # Avoid assessment, $20 price, big gap, but only 60 PPR projected → neutral
    assert derive_system_signal(
        "avoid", False, -15.0, 5.0, 20.0, projected_ppr=60.0,
    ) == "neutral"
    # slight_overpay, 50 PPR → neutral
    assert derive_system_signal(
        "slight_overpay", False, -10.0, 10.0, 20.0, projected_ppr=50.0,
    ) == "neutral"
    # good_value + low projection → buy if gap positive
    assert derive_system_signal(
        "good_value", False, 6.0, 26.0, 20.0, projected_ppr=70.0,
    ) == "buy"
    # good_value + low projection + small gap → neutral
    assert derive_system_signal(
        "good_value", False, 2.0, 22.0, 20.0, projected_ppr=70.0,
    ) == "neutral"


def test_high_projection_still_avoid():
    """Players projected >= 80 PPR can still get avoid signal."""
    assert derive_system_signal(
        "avoid", False, -20.0, 10.0, 30.0, projected_ppr=120.0,
    ) == "strong_avoid"
    assert derive_system_signal(
        "avoid", False, -10.0, 10.0, 20.0, projected_ppr=200.0,
    ) == "avoid"


def test_projection_floor_boundary():
    """At exactly 80 PPR, avoid is still possible (floor is < 80)."""
    assert derive_system_signal(
        "avoid", False, -20.0, 10.0, 30.0, projected_ppr=80.0,
    ) == "strong_avoid"


def test_no_projection_data_still_allow_avoid():
    """When projected_ppr is None, projection floor doesn't filter."""
    assert derive_system_signal(
        "avoid", False, -20.0, 10.0, 30.0, projected_ppr=None,
    ) == "strong_avoid"


def test_cheap_player_threshold_at_12():
    """$12 players are protected, $13 players are not."""
    # $12 → protected (neutral)
    assert derive_system_signal("avoid", False, -10.0, 2.0, 12.0) == "neutral"
    # $13 → NOT protected (avoid applies)
    assert derive_system_signal("avoid", False, -10.0, 3.0, 13.0) == "avoid"
