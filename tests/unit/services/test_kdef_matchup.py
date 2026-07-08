"""
DST matchup-weekly tilt (K/DEF streaming arc, slice 4) — the gentle opponent tilt,
its cap + orientation, the as-of-W-1 opponent-offense signal, and DST-only injection
(kicker + offense untouched, season forward_value preserved). Pure — no DB/network.
"""
from __future__ import annotations

import logging

import pandas as pd

from backend.services.kdef_matchup import (
    DST_TILT_CAP,
    apply_dst_tilt,
    build_offense_signal,
    dst_tilt,
    league_means,
    opponent_by_team,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend

MEANS = {"sacks": 2.0, "gv": 1.0, "pts": 22.0}


def _iv(pid, pos, ppg, fv=50.0):
    return InSeasonValue(
        canonical_player_id=pid, name=pid, position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=10, usage_recent=0.0, usage_prior=0.0, usage_delta=0.0,
        recency_ppg=ppg, expected_ppg=ppg, opportunity_gap=0.0, sustainable=True,
        forward_ppg=ppg, schedule_modifier=0.0, prior_projection=None, prior_weight=0.0,
        name_bias_guard_applied=False, confidence=Confidence.FULL, confidence_reason="",
    )


# ---- the tilt ----
def test_tilt_direction():
    sack_prone = {"sacks_allowed_pg": 3.5, "giveaways_pg": 1.5, "points_pg": 20}
    clean = {"sacks_allowed_pg": 1.0, "giveaways_pg": 0.5, "points_pg": 26}
    assert dst_tilt(sack_prone, MEANS) > 0        # sack/turnover-prone opp -> HIGHER DST
    assert dst_tilt(clean, MEANS) < 0             # clean, low-sack opp -> LOWER DST


def test_average_opponent_yields_zero_tilt():
    avg = {"sacks_allowed_pg": 2.0, "giveaways_pg": 1.0, "points_pg": 22.0}
    assert dst_tilt(avg, MEANS) == 0.0            # exactly league-average -> baseline unchanged


def test_tilt_is_capped():
    extreme = {"sacks_allowed_pg": 6.0, "giveaways_pg": 4.0, "points_pg": 10}
    assert dst_tilt(extreme, MEANS) == DST_TILT_CAP           # gentle by design
    tiny = {"sacks_allowed_pg": 0.0, "giveaways_pg": 0.0, "points_pg": 40}
    assert dst_tilt(tiny, MEANS) == -DST_TILT_CAP


def test_sacks_dominates_giveaways():
    # equal raw deviation on sacks vs giveaways -> sacks moves the tilt more.
    by_sacks = dst_tilt({"sacks_allowed_pg": 4.0, "giveaways_pg": 1.0, "points_pg": 22.0}, MEANS)
    by_gv = dst_tilt({"sacks_allowed_pg": 2.0, "giveaways_pg": 3.0, "points_pg": 22.0}, MEANS)
    assert by_sacks > by_gv


# ---- the as-of-W-1 opponent-offense signal (slice-1 mirror) ----
def _kdef(rows):
    cols = ["position", "nfl_team", "week", "sacks", "interceptions", "fumble_recoveries", "points_allowed"]
    return pd.DataFrame(rows, columns=cols)


def _sched(rows):
    return pd.DataFrame(rows, columns=["home_team", "away_team", "week", "game_type"])


def test_build_offense_signal_mirrors_opponent_dst():
    # wk1 CLE(home) vs LV: CLE DST = 5 sacks/2 INT/1 fum/10 PA; LV DST = 1 sack/0/0/24 PA.
    kdef = _kdef([
        ("DEF", "CLE", 1, 5, 2, 1, 10),
        ("DEF", "LV", 1, 1, 0, 0, 24),
    ])
    sch = _sched([("CLE", "LV", 1, "REG")])
    sig = build_offense_signal(kdef, sch, upto_week=1)
    # LV's offense faced CLE's D: sacks-allowed = CLE's sacks (5), giveaways = CLE INT+fum (3), points = 10.
    assert sig["LV"]["sacks_allowed_pg"] == 5.0
    assert sig["LV"]["giveaways_pg"] == 3.0
    assert sig["LV"]["points_pg"] == 10.0
    # CLE's offense faced LV's D: sacks-allowed = LV's sacks (1), points = 24.
    assert sig["CLE"]["sacks_allowed_pg"] == 1.0 and sig["CLE"]["points_pg"] == 24.0


def test_offense_signal_excludes_week_w_and_after():
    # week 2 data must NOT leak into an as-of-W-1=1 signal (holdout safety).
    kdef = _kdef([
        ("DEF", "CLE", 1, 5, 0, 0, 10),
        ("DEF", "CLE", 2, 0, 0, 0, 30),   # week 2 — must be excluded when upto_week=1
        ("DEF", "LV", 1, 1, 0, 0, 24),
        ("DEF", "LV", 2, 1, 0, 0, 20),
    ])
    sch = _sched([("CLE", "LV", 1, "REG"), ("LV", "CLE", 2, "REG")])
    sig = build_offense_signal(kdef, sch, upto_week=1)
    assert sig["LV"]["games"] == 1 and sig["LV"]["points_pg"] == 10.0   # only wk1


def test_opponent_by_team():
    sch = _sched([("CLE", "LV", 14, "REG"), ("KC", "DEN", 14, "REG"), ("SF", "SEA", 13, "REG")])
    o = opponent_by_team(sch, 14)
    assert o["CLE"] == "LV" and o["LV"] == "CLE" and o["KC"] == "DEN"
    assert "SF" not in o                                # week 13 not included


# ---- DST-only injection ----
def test_apply_tilt_dst_only_kicker_and_offense_untouched():
    values = {"d": _iv("d", "DEF", 7.0), "k": _iv("k", "K", 9.0), "w": _iv("w", "WR", 15.0)}
    signal = {"DAL": {"sacks_allowed_pg": 4.0, "giveaways_pg": 2.0, "points_pg": 18.0},
              "PHI": {"sacks_allowed_pg": 1.0, "giveaways_pg": 0.5, "points_pg": 26.0}}
    out = apply_dst_tilt(values, {"d": "NYG"}, signal, {"NYG": "DAL"}, week=14)
    assert out["d"].forward_ppg != 7.0                  # DST tilted (faces sack-prone DAL)
    assert out["d"].forward_ppg > 7.0
    assert out["k"].forward_ppg == 9.0                  # kicker untouched (no matchup for K)
    assert out["w"].forward_ppg == 15.0                 # offense untouched
    # season forward_value preserved for ALL (the anchor slices 2-3 built)
    assert all(out[p].forward_value == values[p].forward_value for p in values)


def test_dst_with_no_opponent_kept_flat_and_warned(caplog):
    values = {"d": _iv("d", "DEF", 7.0)}
    signal = {"DAL": {"sacks_allowed_pg": 4.0, "giveaways_pg": 2.0, "points_pg": 18.0}}
    with caplog.at_level(logging.WARNING):
        out = apply_dst_tilt(values, {"d": "NYG"}, signal, {}, week=14)   # NYG on bye (no opponent)
    assert out["d"].forward_ppg == 7.0                  # flat baseline, never dropped
    assert "kept at season baseline" in caplog.text     # loud-warned
