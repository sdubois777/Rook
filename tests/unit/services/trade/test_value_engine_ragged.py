"""
Ragged-history contract tests for the in-season value engine.

Irregular player histories — thin samples, byes, rookies, zero-usage weeks,
NaNs, and mid-season team changes — must degrade gracefully (never NaN/crash,
never fabricate a confident trend) and surface a data-sufficiency `confidence`.
One test per step-0 case, plus a regression that #150's full-history fixtures
keep their verdicts at confidence=full.
"""
from __future__ import annotations

import math

import pandas as pd

from backend.services.trade.value_engine import (
    Confidence,
    ValueTrend,
    compute_player_value,
)


def _weeks(snaps, targets, points, *, weeks=None, teams=None, tgts=None, carries=None):
    n = len(snaps)
    weeks = weeks or list(range(1, n + 1))
    data = {
        "week": weeks,
        "snap_pct": snaps,
        "target_share": targets,
        "fantasy_points_ppr": points,
        "targets": tgts if tgts is not None else [0] * n,
        "carries": carries if carries is not None else [0] * n,
    }
    if teams is not None:
        data["nfl_team"] = teams
    return pd.DataFrame(data)


def _numeric_fields(v):
    return [
        v.forward_value, v.usage_recent, v.usage_prior, v.usage_delta,
        v.recency_ppg, v.expected_ppg, v.opportunity_gap, v.forward_ppg,
        v.prior_weight,
    ]


# ---------------------------------------------------------------------------
# 0A — too few games: no fabricated confident trend
# ---------------------------------------------------------------------------
def test_two_game_player_is_limited_with_no_trend_or_flags():
    v = compute_player_value(
        canonical_player_id="a", name="Sophomore", position="WR",
        weeks=_weeks([0.40, 0.90], [0.10, 0.30], [5, 20]), current_week=2,
    )
    assert v.confidence is Confidence.LIMITED
    assert v.value_trend is ValueTrend.STABLE     # cannot form a prior window
    assert v.buy_low is False and v.sell_high is False
    assert not any(math.isnan(x) for x in _numeric_fields(v))


def test_single_game_player_is_insufficient():
    v = compute_player_value(
        canonical_player_id="a1", name="Debut", position="RB",
        weeks=_weeks([0.6], [0.1], [12]), current_week=1,
    )
    assert v.confidence is Confidence.INSUFFICIENT
    assert v.value_trend is ValueTrend.STABLE
    assert v.buy_low is False and v.sell_high is False


# ---------------------------------------------------------------------------
# 0B — bye inside the window: games-played semantics, no false decline
# ---------------------------------------------------------------------------
def test_bye_in_window_does_not_create_false_decline():
    """Flat-but-strong usage with a week-4 bye (no row) must read STABLE on 5
    played games — NOT a decline from a phantom 0-usage bye week."""
    v = compute_player_value(
        canonical_player_id="b", name="Bye Guy", position="WR",
        weeks=_weeks([0.82] * 5, [0.22] * 5, [14] * 5, weeks=[1, 2, 3, 5, 6]),
        current_week=6,
    )
    assert v.games_played == 5            # the bye week is skipped, not counted
    assert v.value_trend is ValueTrend.STABLE
    assert v.value_trend is not ValueTrend.FALLING
    assert v.confidence is Confidence.FULL


# ---------------------------------------------------------------------------
# 0C — null prior (rookie / no projection)
# ---------------------------------------------------------------------------
def test_null_prior_rookie_runs_and_uses_pure_inseason():
    v = compute_player_value(
        canonical_player_id="c", name="Rookie", position="RB",
        weeks=_weeks([0.5] * 5, [0.10] * 5, [10] * 5), current_week=5,
        prior_projection_ppg=None,
    )
    assert v.prior_projection is None
    assert v.prior_weight == 0.0          # no prior to lean on → pure in-season
    assert v.forward_ppg == v.recency_ppg
    assert not any(math.isnan(x) for x in _numeric_fields(v))


# ---------------------------------------------------------------------------
# 0D — zero denominator / NaN usage
# ---------------------------------------------------------------------------
def test_zero_usage_window_no_div_by_zero():
    v = compute_player_value(
        canonical_player_id="d", name="Deep Bench", position="WR",
        weeks=_weeks([0.0] * 4, [0.0] * 4, [0.0] * 4), current_week=4,
    )
    assert v.value_trend is ValueTrend.STABLE
    assert v.forward_value == 0.0          # below replacement, clamped — defined
    assert not any(math.isnan(x) for x in _numeric_fields(v))


def test_nan_usage_does_not_propagate_to_output():
    v = compute_player_value(
        canonical_player_id="d2", name="NaN Week", position="WR",
        weeks=_weeks([0.5, float("nan"), 0.6, 0.7, 0.8], [0.1] * 5, [8, 9, 10, 11, 12]),
        current_week=5,
    )
    assert not any(math.isnan(x) for x in _numeric_fields(v))


# ---------------------------------------------------------------------------
# 0E — team change across the window
# ---------------------------------------------------------------------------
def test_team_change_suppresses_direction_but_keeps_limited_and_raw_trend():
    """A team change in the window corrupts the share delta (two offenses), so the
    actionable buy/sell flags are SUPPRESSED — but, unlike insufficient,
    confidence stays `limited` and value_trend keeps its RAW direction as a
    transparency sub-signal."""
    traded = _weeks(
        [0.50, 0.50, 0.50, 0.85, 0.90],
        [0.10, 0.10, 0.10, 0.26, 0.28],
        [6, 6, 6, 16, 18],
        teams=["AAA", "AAA", "AAA", "BBB", "BBB"],
    )
    v = compute_player_value(
        canonical_player_id="e", name="Traded WR", position="WR",
        weeks=traded, current_week=5,
    )
    assert v.confidence is Confidence.LIMITED          # NOT insufficient
    assert v.buy_low is False and v.sell_high is False  # direction suppressed
    assert v.value_trend is ValueTrend.RISING          # raw direction retained
    assert "team change" in v.confidence_reason and v.why == v.confidence_reason

    # The suppression cause is distinct from insufficient's (different message).
    insufficient = compute_player_value(
        canonical_player_id="ins", name="One Game", position="WR",
        weeks=_weeks([0.5], [0.1], [10]), current_week=1,
    )
    assert insufficient.confidence is Confidence.INSUFFICIENT
    assert insufficient.confidence_reason != v.confidence_reason
    assert "team change" not in insufficient.confidence_reason

    # Same trajectory, SAME team → flag is NOT suppressed (proves the team change,
    # not the trajectory or game count, is what neutralizes the direction).
    same_team = traded.copy()
    same_team["nfl_team"] = "AAA"
    v2 = compute_player_value(
        canonical_player_id="e2", name="Stable WR", position="WR",
        weeks=same_team, current_week=5,
    )
    assert v2.confidence is Confidence.FULL
    assert v2.value_trend is ValueTrend.RISING and v2.buy_low is True


# ---------------------------------------------------------------------------
# Regression — #150 full-history verdicts unchanged, confidence=full
# ---------------------------------------------------------------------------
def test_full_history_rising_keeps_buy_low_at_full_confidence():
    v = compute_player_value(
        canonical_player_id="rise", name="Rising WR", position="WR",
        weeks=_weeks(
            [0.40, 0.45, 0.50, 0.80, 0.90],
            [0.08, 0.10, 0.12, 0.22, 0.26],
            [5, 6, 7, 12, 14],
            tgts=[4, 5, 6, 11, 13],
        ),
        current_week=5,
    )
    assert v.confidence is Confidence.FULL
    assert v.value_trend is ValueTrend.RISING
    assert v.buy_low is True and v.sell_high is False


def test_full_history_four_game_sell_keeps_verdict_at_full_confidence():
    """#150's 4-game declining fixture stays FULL (≥4 same-team games) and the
    sell_high verdict is unchanged."""
    v = compute_player_value(
        canonical_player_id="fade", name="Star WR", position="WR",
        weeks=_weeks([0.90, 0.80, 0.55, 0.50], [0.28, 0.24, 0.13, 0.11],
                     [18, 16, 9, 8], tgts=[9, 8, 4, 4]),
        current_week=4, prior_projection_ppg=19.0,
    )
    assert v.confidence is Confidence.FULL
    assert v.value_trend is ValueTrend.FALLING
    assert v.sell_high is True
    assert v.name_bias_guard_applied is True
