"""
Unit tests for the in-season value engine (backend/services/trade/value_engine.py).

All fixtures are FIXED, hand-built per-week lines (the shape the #149 layer
emits). The headline assertions exercise the differentiator: value follows usage
TRAJECTORY and current production, never the player's name/reputation.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.trade.league_state import (
    LeagueState,
    RosterPlayer,
    TeamState,
)
from backend.services.trade.value_engine import (
    ValueTrend,
    _played_weeks,
    compute_player_value,
    evaluate_league,
    usage_trend,
)


def _weeks(snaps, targets, points, *, carries=None, tgts=None, start_week=1):
    """Build a per-player weekly frame from parallel lists."""
    n = len(snaps)
    carries = carries or [0] * n
    tgts = tgts if tgts is not None else [0] * n
    return pd.DataFrame({
        "week": list(range(start_week, start_week + n)),
        "snap_pct": snaps,
        "target_share": targets,
        "fantasy_points_ppr": points,
        "targets": tgts,
        "carries": carries,
    })


# ---------------------------------------------------------------------------
# Trend math — last-2 vs prior-3 direction on a constructed series
# ---------------------------------------------------------------------------
def test_usage_trend_rising_when_recent_two_exceed_prior_three():
    df = _played_weeks(
        _weeks([0.40, 0.45, 0.50, 0.80, 0.90], [0.08, 0.10, 0.12, 0.22, 0.26], [5] * 5),
        current_week=5,
    )
    recent, prior, delta, trend = usage_trend(df)
    assert trend is ValueTrend.RISING
    assert delta > 0 and recent > prior


def test_usage_trend_falling_when_recent_two_below_prior_three():
    df = _played_weeks(
        _weeks([0.90, 0.85, 0.80, 0.50, 0.45], [0.28, 0.26, 0.24, 0.12, 0.10], [10] * 5),
        current_week=5,
    )
    _, _, delta, trend = usage_trend(df)
    assert trend is ValueTrend.FALLING
    assert delta < 0


def test_usage_trend_stable_when_flat():
    df = _played_weeks(
        _weeks([0.6] * 5, [0.15] * 5, [10] * 5), current_week=5,
    )
    _, _, delta, trend = usage_trend(df)
    assert trend is ValueTrend.STABLE
    assert delta == pytest.approx(0.0)


def test_played_weeks_respects_current_week_anchor():
    """Weeks after the anchor are excluded — engine is week-agnostic."""
    df = _played_weeks(_weeks([0.6] * 6, [0.15] * 6, [10] * 6), current_week=4)
    assert df["week"].max() == 4
    assert len(df) == 4


# ---------------------------------------------------------------------------
# Buy-low: rising usage
# ---------------------------------------------------------------------------
def test_rising_usage_flags_buy_low():
    """A WR whose snap% + target share climb over the last two weeks is BUY-LOW,
    even on still-modest points."""
    weeks = _weeks(
        snaps=[0.40, 0.45, 0.50, 0.80, 0.90],
        targets=[0.08, 0.10, 0.12, 0.22, 0.26],
        points=[5, 6, 7, 12, 14],
        tgts=[4, 5, 6, 11, 13],
    )
    v = compute_player_value(
        canonical_player_id="rise", name="Rising WR", position="WR",
        weeks=weeks, current_week=5,
    )
    assert v.value_trend is ValueTrend.RISING
    assert v.buy_low is True
    assert v.sell_high is False
    assert "rising" in v.why


def test_opportunity_gap_flags_buy_low_on_high_volume_low_output():
    """Heavy volume but suppressed output → production should catch up → buy."""
    weeks = _weeks(
        snaps=[0.85] * 5, targets=[0.24] * 5, points=[5, 4, 6, 5, 4],
        tgts=[10, 11, 10, 12, 11],
    )
    v = compute_player_value(
        canonical_player_id="vol", name="Volume WR", position="WR",
        weeks=weeks, current_week=5,
    )
    assert v.opportunity_gap <= -4.0
    assert v.buy_low is True


# ---------------------------------------------------------------------------
# Sell-high: declining usage on a name brand + the name-bias guard
# ---------------------------------------------------------------------------
def test_declining_name_brand_flags_sell_high_and_discounts_reputation():
    """A reputation player (high preseason prior) whose role is decaying is
    SELL-HIGH, and the name-bias guard discounts the prior so reputation cannot
    prop the forward value back up."""
    weeks = _weeks(
        snaps=[0.90, 0.80, 0.55, 0.50],
        targets=[0.28, 0.24, 0.13, 0.11],
        points=[18, 16, 9, 8],
        tgts=[9, 8, 4, 4],
    )
    v = compute_player_value(
        canonical_player_id="fade", name="Star WR", position="WR",
        weeks=weeks, current_week=4,
        prior_projection_ppg=19.0,   # big preseason reputation
    )
    assert v.value_trend is ValueTrend.FALLING
    assert v.sell_high is True
    assert v.name_bias_guard_applied is True
    assert v.prior_weight < 0.15                  # reputation heavily discounted
    # A 19-ppg reputation would scale to ~73/100; the decayed role pins it far lower.
    assert v.forward_value < 40
    assert "down-weighted" in v.why


def test_unsustainable_hot_scoring_flags_sell_high():
    """High points on low volume + falling usage = TD-variance → sell."""
    weeks = _weeks(
        snaps=[0.70, 0.65, 0.45, 0.40],
        targets=[0.18, 0.16, 0.08, 0.07],
        points=[8, 9, 19, 20],
        tgts=[6, 5, 3, 3],
    )
    v = compute_player_value(
        canonical_player_id="hot", name="Hot WR", position="WR",
        weeks=weeks, current_week=4,
    )
    assert v.value_trend is ValueTrend.FALLING
    assert v.opportunity_gap >= 4.0
    assert v.sustainable is False
    assert v.sell_high is True


# ---------------------------------------------------------------------------
# Prior vs in-season CONFLICT — value follows the in-season data
# ---------------------------------------------------------------------------
def test_high_prior_but_weak_inseason_value_follows_inseason():
    """Preseason says stud (prior 20 ppg → would scale ~80/100), but six weeks of
    middling usage/production say otherwise. With a full in-season sample the
    prior washes out and forward_value tracks the in-season reality."""
    weeks = _weeks(
        snaps=[0.50] * 6, targets=[0.10] * 6,
        points=[11, 12, 10, 11, 12, 11], tgts=[5] * 6,
    )
    v = compute_player_value(
        canonical_player_id="bust", name="Preseason Stud", position="WR",
        weeks=weeks, current_week=6,
        prior_projection_ppg=20.0,
    )
    assert v.prior_weight == pytest.approx(0.0)     # 6 games ≥ full-in-season
    # in-season ~11 ppg → ~20/100, nowhere near the prior-implied ~80.
    assert v.forward_value < 40
    assert abs(v.forward_ppg - v.recency_ppg) < 0.5  # forward == in-season form


# ---------------------------------------------------------------------------
# Name-bias guard as a real, testable code path: value is name-independent
# ---------------------------------------------------------------------------
def test_value_is_independent_of_player_name():
    """Identical usage/production with different names/ids → identical value.
    Proves value derives from the data, never the name."""
    weeks_a = _weeks([0.6, 0.62, 0.7, 0.75], [0.18, 0.2, 0.22, 0.24], [12, 13, 15, 16])
    weeks_b = weeks_a.copy()
    common = dict(position="WR", weeks=weeks_a, current_week=4, prior_projection_ppg=14.0)
    famous = compute_player_value(canonical_player_id="x", name="Superstar Famous", **{**common, "weeks": weeks_a})
    nobody = compute_player_value(canonical_player_id="y", name="Anonymous Scrub", **{**common, "weeks": weeks_b})
    assert famous.forward_value == nobody.forward_value
    assert famous.value_trend == nobody.value_trend
    assert famous.buy_low == nobody.buy_low and famous.sell_high == nobody.sell_high


def test_no_inseason_data_falls_back_to_prior_only():
    v = compute_player_value(
        canonical_player_id="z", name="Injured", position="RB",
        weeks=_weeks([], [], []), current_week=5, prior_projection_ppg=16.0,
    )
    assert v.games_played == 0
    assert v.value_trend is ValueTrend.STABLE
    assert v.forward_ppg == pytest.approx(16.0)


# ---------------------------------------------------------------------------
# League-level convenience
# ---------------------------------------------------------------------------
def test_evaluate_league_values_every_rostered_player():
    weekly = pd.concat([
        _weeks([0.4, 0.5, 0.6, 0.8, 0.9], [0.1, 0.12, 0.14, 0.22, 0.26], [6, 7, 8, 13, 15]).assign(canonical_player_id="rise"),
        _weeks([0.9, 0.85, 0.8, 0.5, 0.45], [0.28, 0.26, 0.24, 0.12, 0.1], [18, 16, 14, 9, 8]).assign(canonical_player_id="fade"),
    ], ignore_index=True)
    state = LeagueState(
        season=2025, week=5,
        teams=(
            TeamState("t1", "Mine", is_me=True, roster=(
                RosterPlayer("rise", "Rising WR", "WR"),
            )),
            TeamState("t2", "Theirs", is_me=False, roster=(
                RosterPlayer("fade", "Fading WR", "WR"),
            )),
        ),
    )
    values = evaluate_league(state, weekly)
    assert set(values) == {"rise", "fade"}
    assert values["rise"].value_trend is ValueTrend.RISING
    assert values["rise"].buy_low is True
    assert values["fade"].value_trend is ValueTrend.FALLING
    assert values["fade"].sell_high is True
