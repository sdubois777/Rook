"""
K/DEF value anchors (slice 2) — K and DEF are first-class in the in-season value
engine, and OFFENSE is byte-identical (the regression that matters most).
"""
from __future__ import annotations

import pandas as pd

from backend.services.trade.value_engine import (
    _PPG_ANCHORS,
    STARTERS_PER_POS,
    Confidence,
    InSeasonValue,
    ValueTrend,
    compute_player_value,
    derive_anchors,
    replacement_ppg_by_position,
)


def _iv(pid, pos, ppg):
    return InSeasonValue(
        canonical_player_id=pid, name=pid, position=pos, forward_value=50.0,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=10, usage_recent=0.0, usage_prior=0.0, usage_delta=0.0,
        recency_ppg=ppg, expected_ppg=ppg, opportunity_gap=0.0, sustainable=True,
        forward_ppg=ppg, schedule_modifier=0.0, prior_projection=None, prior_weight=0.0,
        name_bias_guard_applied=False, confidence=Confidence.FULL, confidence_reason="",
    )


# ---- K/DEF are configured positions ---------------------------------------
def test_kdef_in_starters_and_anchors():
    assert STARTERS_PER_POS.get("K") == 1 and STARTERS_PER_POS.get("DEF") == 1
    assert "K" in _PPG_ANCHORS and "DEF" in _PPG_ANCHORS


def test_derive_anchors_includes_kdef_and_leaves_offense_identical():
    offense = {"QB": [22, 20, 18], "RB": [16, 13, 10], "WR": [15, 12, 9], "TE": [9, 7, 5]}
    combined = {**offense, "K": [11, 9, 8, 7], "DEF": [12, 9, 7, 5]}
    a_off = derive_anchors(offense)
    a_all = derive_anchors(combined)
    # offense byte-identical whether or not K/DEF are in the pool
    for pos in ("QB", "RB", "WR", "TE"):
        assert a_all[pos] == a_off[pos]
    # K/DEF now produced (fallback here — sparse pool — but PRESENT)
    assert a_all["K"] == _PPG_ANCHORS["K"] and a_all["DEF"] == _PPG_ANCHORS["DEF"]


def test_derive_anchors_derives_real_kdef_from_a_full_pool():
    # A full K pool (>= cutoff + margin) derives a REAL anchor, not the fallback.
    kvals = [15, 14, 13, 12, 11, 10.5, 10, 9.5, 9, 8.5, 8, 7.5, 7, 6.5, 6, 5.5, 5, 4]
    out = derive_anchors({"K": kvals})
    assert out["K"] != _PPG_ANCHORS["K"]            # derived, not fallback
    assert out["K"][0] < out["K"][1]                 # replacement < elite


def test_replacement_ppg_returns_kdef_alongside_offense():
    values = {
        "q": _iv("q", "QB", 18), "r": _iv("r", "RB", 12), "w": _iv("w", "WR", 11),
        "t": _iv("t", "TE", 7), "k": _iv("k", "K", 9), "d": _iv("d", "DEF", 8),
    }
    repl = replacement_ppg_by_position(values)
    assert set(repl) == {"QB", "RB", "WR", "TE", "K", "DEF"}
    assert all(isinstance(v, float) for v in repl.values())


# ---- compute_player_value on a K weekly series ----------------------------
def test_compute_value_kicker_non_degenerate_level_based():
    weeks = pd.DataFrame({
        "week": [1, 2, 3, 4, 5, 6],
        "fantasy_points_ppr": [8.0, 10.0, 12.0, 9.0, 11.0, 10.0],
        "snap_pct": 0.0, "target_share": 0.0, "targets": 0, "carries": 0,
    })
    v = compute_player_value(
        canonical_player_id="k1", name="K1", position="K",
        weeks=weeks, current_week=6, anchors={"K": (6.0, 14.0)},
    )
    assert v.games_played == 6 and v.confidence is Confidence.FULL
    assert v.forward_ppg > 8.0                       # real level, not 0/degenerate
    assert 0.0 < v.forward_value <= 100.0
    # Zero volume must NOT trigger an opp-gap crater — value tracks the level.
    assert abs(v.forward_ppg - v.recency_ppg) < 3.0
