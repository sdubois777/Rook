"""
Unit tests for the weekly RAW K/DEF stat-line layer (compute_weekly_kdef +
build_weekly_kdef in backend/integrations/nfl_weekly.py).

Synthetic PBP with hand-known plays — no network, no DB — that LOCKS:
  * the real CLE wk12 DST line (10 sacks / 1 fum-rec / 10 pts / 268 yds) and the
    real McPherson 6-of-6 FG line [24,31,33,41,42,52] + 2/2 XP (both box-score-
    verified against live 2025 PBP during the build),
  * points-allowed = opponent final score, with the DST's OWN def/ST TD
    self-excluded and the opponent's non-offensive points stored as components,
  * the identity joins (DST by team_abbr, kicker by gsis crosswalk) + loud-warn
    drop of any unmapped team / kicker.

A guarded real-cache spot-check re-verifies the CLE/McPherson lines end-to-end if
the gitignored weekly_kdef_2025 cache is present (skips cleanly in CI).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest

from backend.integrations.nfl_weekly import (
    build_weekly_kdef,
    compute_weekly_kdef,
)

CACHE_DIR = Path("data/cache")


# ---------------------------------------------------------------------------
# Synthetic PBP builder (only the columns the K/DEF path reads)
# ---------------------------------------------------------------------------
def _row(game_id, week, home_team, away_team, home_score, away_score, **kw):
    base = dict(
        season_type="REG", game_id=game_id, week=week,
        home_team=home_team, away_team=away_team,
        home_score=home_score, away_score=away_score,
        posteam=None, defteam=None,
        sack=0, interception=0, safety=0, touchdown=0, td_team=None,
        yards_gained=0, fumble_recovery_1_team=None,
        kicker_player_id=None, kicker_player_name=None,
        field_goal_attempt=0, field_goal_result=None, kick_distance=None,
        extra_point_attempt=0, extra_point_result=None,
    )
    base.update(kw)
    return base


def _synthetic_pbp():
    rows = []
    # --- Game 1: CLE @ LV, wk12. game_id season_week_away_home => CLE away, LV home.
    #     Final CLE 24 - LV 10 => home_score(LV)=10, away_score(CLE)=24.
    g1 = dict(game_id="2025_12_CLE_LV", week=12, home_team="LV", away_team="CLE",
              home_score=10, away_score=24)
    for _ in range(10):  # 10 sacks by CLE
        rows.append(_row(**g1, posteam="LV", defteam="CLE", sack=1))
    rows.append(_row(**g1, posteam="LV", defteam="CLE", fumble_recovery_1_team="CLE"))
    rows.append(_row(**g1, posteam="LV", defteam="CLE", yards_gained=268))  # all yards allowed
    # McPherson kicking for CLE: 6 made FG + 2 good XP (posteam=CLE on kicks).
    for dist in (33, 24, 52, 41, 31, 42):  # unsorted on purpose
        rows.append(_row(**g1, posteam="CLE", defteam="LV",
                         kicker_player_id="00-0036854", kicker_player_name="E.McPherson",
                         field_goal_attempt=1, field_goal_result="made", kick_distance=dist))
    for _ in range(2):
        rows.append(_row(**g1, posteam="CLE", defteam="LV",
                         kicker_player_id="00-0036854", kicker_player_name="E.McPherson",
                         extra_point_attempt=1, extra_point_result="good"))
    # one missed FG (must not count as made, no distance in the list)
    rows.append(_row(**g1, posteam="CLE", defteam="LV",
                     kicker_player_id="00-0036854", kicker_player_name="E.McPherson",
                     field_goal_attempt=1, field_goal_result="missed", kick_distance=55))

    # --- Game 2: DAL @ SEA, wk10. SEA scores a DEF TD + a safety on DAL.
    #     Final SEA 30 - DAL 22 => home_score(SEA)=30, away_score(DAL)=22.
    g2 = dict(game_id="2025_10_DAL_SEA", week=10, home_team="SEA", away_team="DAL",
              home_score=30, away_score=22)
    rows.append(_row(**g2, posteam="DAL", defteam="SEA", touchdown=1, td_team="SEA"))  # SEA pick-6
    rows.append(_row(**g2, posteam="DAL", defteam="SEA", safety=1))                      # SEA safety
    rows.append(_row(**g2, posteam="SEA", defteam="DAL", yards_gained=300))              # DAL allows yards
    return pd.DataFrame([_row(**g1, posteam="LV", defteam="CLE")] * 0 + rows)


def _raw():
    return compute_weekly_kdef(2025, pbp=_synthetic_pbp(), use_cache=False)


def _line(raw, position, **match):
    df = raw[raw["position"] == position]
    for col, val in match.items():
        df = df[df[col] == val]
    assert len(df) == 1, f"expected 1 {position} row for {match}, got {len(df)}"
    return df.iloc[0]


# ---------------------------------------------------------------------------
# DST — the real CLE wk12 line
# ---------------------------------------------------------------------------
def test_dst_cle_wk12_matches_box_score():
    cle = _line(_raw(), "DEF", defteam="CLE", week=12)
    assert int(cle["sacks"]) == 10
    assert int(cle["interceptions"]) == 0
    assert int(cle["fumble_recoveries"]) == 1
    assert int(cle["safeties"]) == 0
    assert int(cle["def_st_tds"]) == 0
    assert int(cle["yards_allowed"]) == 268
    assert int(cle["points_allowed"]) == 10          # LV's final score
    assert int(cle["opp_nonoffense_tds"]) == 0
    assert int(cle["opp_safeties"]) == 0


# ---------------------------------------------------------------------------
# K — the real McPherson line (distances kept per made kick, sorted, unbucketed)
# ---------------------------------------------------------------------------
def test_k_mcpherson_line():
    mc = _line(_raw(), "K", player_id="00-0036854", week=12)
    assert int(mc["fg_att"]) == 7 and int(mc["fg_made"]) == 6 and int(mc["fg_missed"]) == 1
    assert list(mc["fg_made_distances"]) == [24, 31, 33, 41, 42, 52]
    assert int(mc["xp_att"]) == 2 and int(mc["xp_made"]) == 2


# ---------------------------------------------------------------------------
# points-allowed self-exclusion + opponent non-offense components
# ---------------------------------------------------------------------------
def test_def_st_td_counted_but_not_inflating_points_allowed():
    raw = _raw()
    sea = _line(raw, "DEF", defteam="SEA", week=10)
    assert int(sea["def_st_tds"]) == 1        # the pick-6 is counted
    assert int(sea["safeties"]) == 1
    assert int(sea["points_allowed"]) == 22   # DAL's final — NOT inflated by SEA's own TD
    # DAL carries the opponent's non-offensive points as components (for slice 2).
    dal = _line(raw, "DEF", defteam="DAL", week=10)
    assert int(dal["opp_nonoffense_tds"]) == 1
    assert int(dal["opp_safeties"]) == 1


# ---------------------------------------------------------------------------
# identity joins + loud-warn drop
# ---------------------------------------------------------------------------
_DST_MAP = {
    "CLE": ("cle-uuid", "Cleveland Browns"), "LV": ("lv-uuid", "Las Vegas Raiders"),
    "SEA": ("sea-uuid", "Seattle Seahawks"), "DAL": ("dal-uuid", "Dallas Cowboys"),
}
_BRIDGE = pd.DataFrame([
    {"gsis_id": "00-0036854", "sleeper_id": "7839", "sportradar_id": None,
     "position": "PK", "name": "Evan McPherson"},
])
_MAPS = {"sleeper": {"7839": "mcp-uuid"}, "sportradar": {}, "gsis": {}}


def test_build_resolves_dst_by_team_and_kicker_by_crosswalk():
    built = build_weekly_kdef(2025, _MAPS, _DST_MAP, bridge=_BRIDGE, kdef_raw=_raw())
    cle = _line(built, "DEF", canonical_player_id="cle-uuid", week=12)
    assert cle["player_name"] == "Cleveland Browns"      # name from the DEF Player row
    mc = _line(built, "K", canonical_player_id="mcp-uuid", week=12)
    assert int(mc["fg_made"]) == 6                        # gsis 00-0036854 -> sleeper 7839 -> uuid


def test_unmapped_team_and_kicker_are_loud_warned_and_dropped(caplog):
    dst_map = {k: v for k, v in _DST_MAP.items() if k != "DAL"}   # drop DAL
    empty_bridge = pd.DataFrame(columns=["gsis_id", "sleeper_id", "sportradar_id"])
    with caplog.at_level(logging.WARNING):
        built = build_weekly_kdef(2025, {"sleeper": {}, "sportradar": {}, "gsis": {}},
                                  dst_map, bridge=empty_bridge, kdef_raw=_raw())
    assert "DAL" in caplog.text and "DEF" in caplog.text          # team loud-warn
    assert "kicker-week" in caplog.text                            # kicker loud-warn
    assert (built["position"] == "K").sum() == 0                   # no kicker resolved (empty bridge)
    assert not ((built["position"] == "DEF") & (built["nfl_team"] == "DAL")).any()  # DAL dropped


# ---------------------------------------------------------------------------
# Guarded real-2025 spot-check (skips in CI where the cache is absent)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not (CACHE_DIR / "weekly_kdef_2025.parquet").exists(),
                    reason="weekly_kdef_2025 cache not present (real-data spot-check)")
def test_real_2025_cle_and_mcpherson_lines():
    raw = compute_weekly_kdef(2025)  # reads the cache
    cle = _line(raw, "DEF", defteam="CLE", week=12)
    assert int(cle["sacks"]) == 10 and int(cle["points_allowed"]) == 10 and int(cle["yards_allowed"]) == 268
    mc = _line(raw, "K", player_id="00-0036854", week=13)
    assert list(mc["fg_made_distances"]) == [24, 31, 33, 41, 42, 52] and int(mc["xp_made"]) == 2
