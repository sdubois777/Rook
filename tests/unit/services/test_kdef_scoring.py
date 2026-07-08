"""Standard K/DST scoring (backend/services/kdef_scoring.py) — pure, hand-checked."""
from __future__ import annotations

import pandas as pd

from backend.services.kdef_scoring import (
    dst_points_allowed_score,
    fg_distance_score,
    kdef_value_frame,
    score_dst_line,
    score_k_line,
    score_weekly_kdef,
)


def _dst(**kw):
    base = dict(sacks=0, interceptions=0, fumble_recoveries=0, safeties=0,
               def_st_tds=0, points_allowed=0)
    base.update(kw)
    return base


def _k(**kw):
    base = dict(fg_made_distances=[], fg_missed=0, fg_blocked=0,
               xp_made=0, xp_missed=0, xp_blocked=0)
    base.update(kw)
    return base


# ---- DST ------------------------------------------------------------------
def test_dst_cle_wk12_line_scores_16():
    # 10 sacks (10) + 1 fum-rec (2) + PA 10 -> tier 7-13 (4) = 16.
    assert score_dst_line(_dst(sacks=10, fumble_recoveries=1, points_allowed=10)) == 16.0


def test_dst_per_event_and_shutout():
    # shutout (PA 0 = 10) + 2 sacks (2) + 1 INT (2) + 1 safety (2) + 1 def TD (6) = 22.
    assert score_dst_line(_dst(sacks=2, interceptions=1, safeties=1, def_st_tds=1,
                               points_allowed=0)) == 22.0


def test_points_allowed_tiers():
    assert dst_points_allowed_score(0) == 10.0
    assert dst_points_allowed_score(6) == 7.0
    assert dst_points_allowed_score(7) == 4.0 and dst_points_allowed_score(13) == 4.0
    assert dst_points_allowed_score(14) == 1.0 and dst_points_allowed_score(20) == 1.0
    assert dst_points_allowed_score(21) == 0.0 and dst_points_allowed_score(27) == 0.0
    assert dst_points_allowed_score(28) == -1.0 and dst_points_allowed_score(34) == -1.0
    assert dst_points_allowed_score(35) == -4.0 and dst_points_allowed_score(59) == -4.0


# ---- K --------------------------------------------------------------------
def test_k_mcpherson_wk13_scores_24():
    # FG [24,31,33,41,42,52] = 3+3+3+4+4+5 = 22; XP 2 = 2 -> 24.
    assert score_k_line(_k(fg_made_distances=[24, 31, 33, 41, 42, 52], xp_made=2)) == 24.0


def test_fg_distance_bands():
    assert fg_distance_score(39) == 3.0 and fg_distance_score(40) == 4.0
    assert fg_distance_score(49) == 4.0 and fg_distance_score(50) == 5.0
    assert fg_distance_score(63) == 5.0


def test_k_misses_penalized():
    # one 45-yd make (4) + one missed FG (-1) + 3 XP (3) + 1 missed XP (-1) = 5.
    assert score_k_line(_k(fg_made_distances=[45], fg_missed=1, xp_made=3, xp_missed=1)) == 5.0


# ---- frame helpers --------------------------------------------------------
def test_score_weekly_kdef_and_value_frame_shape():
    raw = pd.DataFrame([
        dict(canonical_player_id="cle", player_name="Cleveland Browns", position="DEF",
             nfl_team="CLE", season=2025, week=12, sacks=10, interceptions=0,
             fumble_recoveries=1, safeties=0, def_st_tds=0, points_allowed=10,
             fg_made_distances=[], fg_missed=0, fg_blocked=0, xp_made=0, xp_missed=0, xp_blocked=0),
        dict(canonical_player_id="mcp", player_name="E.McPherson", position="K",
             nfl_team="CIN", season=2025, week=13, sacks=0, interceptions=0,
             fumble_recoveries=0, safeties=0, def_st_tds=0, points_allowed=0,
             fg_made_distances=[24, 31, 33, 41, 42, 52], fg_missed=0, fg_blocked=0,
             xp_made=2, xp_missed=0, xp_blocked=0),
    ])
    scored = score_weekly_kdef(raw)
    assert dict(zip(scored.canonical_player_id, scored.fantasy_points)) == {"cle": 16.0, "mcp": 24.0}

    vf = kdef_value_frame(scored)
    # engine schema: fantasy_points_ppr carries the score; usage columns are zero.
    assert list(vf[vf.canonical_player_id == "cle"]["fantasy_points_ppr"]) == [16.0]
    for col in ("snap_pct", "target_share", "targets", "carries"):
        assert (vf[col] == 0.0).all()
    assert set(vf["position"]) == {"DEF", "K"}
