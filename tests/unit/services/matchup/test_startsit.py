"""
Tier-1 start/sit reasoning — injury-aware optimal lineup (Out/IR excluded, Q/D
flagged), per-starter matchup grade (covered positions only), missing-team BYE,
selective founded bench swaps, and the as-of-week point-in-time def-grade filter.
All pure — no DB/network (data sources monkeypatched for the point-in-time test).
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd

from backend.services.matchup.startsit import (
    available_lineup_roster,
    build_start_sit,
)
from backend.services.trade.league_state import RosterPlayer, TeamState
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _iv(pid, pos, ppg):
    return InSeasonValue(
        canonical_player_id=pid, name=pid, position=pos, forward_value=ppg * 5,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="", games_played=10,
        usage_recent=0.0, usage_prior=0.0, usage_delta=0.0, recency_ppg=ppg, expected_ppg=ppg,
        opportunity_gap=0.0, sustainable=True, forward_ppg=ppg, schedule_modifier=0.0,
        prior_projection=None, prior_weight=0.0, name_bias_guard_applied=False,
        confidence=Confidence.FULL, confidence_reason="",
    )


def _def_grades(rows):
    # (defense_team, position, grade, rank)
    return pd.DataFrame(
        [{"defense_team": d, "position": p, "grade": g, "rank": r, "ppr_per_game": 0.0}
         for d, p, g, r in rows]
    )


# A team: QB, 2 RB, 3 WR, 1 TE, 1 K, 1 DEF + bench. Each RosterPlayer carries nfl_team
# + injury_status; values carry position + forward_ppg.
def _team(players):
    # players: list of (pid, pos, ppg, nfl_team, injury_status)
    roster = tuple(RosterPlayer(pid, pid, pos, nfl_team=team, injury_status=inj)
                   for pid, pos, ppg, team, inj in players)
    values = {pid: _iv(pid, pos, ppg) for pid, pos, ppg, team, inj in players}
    return TeamState("me", "You", True, roster), values


_BASE = [
    ("qb", "QB", 20, "BUF", None),
    ("rb1", "RB", 15, "SF", None), ("rb2", "RB", 12, "DET", None),
    ("wr1", "WR", 16, "MIA", None), ("wr2", "WR", 13, "CIN", None), ("wr3", "WR", 11, "LAC", None),
    ("te", "TE", 10, "LV", None),
    ("k", "K", 8, "DEN", None), ("def", "DEF", 7, "PIT", None),
    ("rb_bench", "RB", 11, "NYJ", None),    # takes FLEX (11 > wr_bench 10.5)
    ("wr_bench", "WR", 10.5, "GB", None),   # genuine bench WR, near wr3 (11) on value
]
_NFL_OPP = {"BUF": "NE", "SF": "SEA", "DET": "GB", "MIA": "NYJ", "CIN": "BAL",
            "LAC": "KC", "LV": "DEN", "DEN": "LV", "PIT": "CLE", "GB": "CHI", "NYJ": "MIA"}


def test_lineup_is_slot_legal_all_slots_shown():
    # The panel shows the REAL slot-legal lineup (1QB/2RB/3WR/1TE/1FLEX/1K/1DEF), one
    # player per slot including QB/K/DEF — not a top-N-by-value WR/RB/TE list.
    team, values = _team(_BASE)
    ss = build_start_sit(team, values, _def_grades([]), _NFL_OPP)
    slots = [s.slot for s in ss.starters]
    assert slots == ["QB", "RB1", "RB2", "WR1", "WR2", "WR3", "TE", "K", "DEF", "FLEX"]
    assert len(ss.starters) == 10                     # exactly the slots — no extra players
    counts = {}
    for s in ss.starters:
        counts[s.position] = counts.get(s.position, 0) + 1
    assert counts["QB"] == 1 and counts["K"] == 1 and counts["DEF"] == 1   # NOT missing QB/K/DEF
    assert counts["TE"] == 1
    # 2 dedicated RB + 3 dedicated WR + 1 FLEX (flex-eligible) = the covered starters.
    assert counts["RB"] + counts["WR"] + counts.get("TE", 0) == 7


def test_two_wr_rules_seat_two_dedicated_wr_plus_flex():
    # With the league's real 2-WR config (via lineup_rules_from_slots), the lineup has
    # WR1/WR2 + FLEX — NOT WR1/WR2/WR3 (the 3-WR DEFAULT bug).
    from backend.services.trade.lineup import lineup_rules_from_slots

    team, values = _team(_BASE)
    rules = lineup_rules_from_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1})
    ss = build_start_sit(team, values, _def_grades([]), _NFL_OPP, rules)
    slots = [s.slot for s in ss.starters]
    assert slots == ["QB", "RB1", "RB2", "WR1", "WR2", "TE", "K", "DEF", "FLEX"]
    assert [s for s in slots if s.startswith("WR")] == ["WR1", "WR2"]   # 2 dedicated WR, no WR3


def test_covered_starters_graded_uncovered_positions_not():
    team, values = _team(_BASE)
    grades = _def_grades([("KC", "WR", "tough", 30), ("BAL", "WR", "favorable", 2)])
    ss = build_start_sit(team, values, grades, _NFL_OPP)
    by = {s.name: s for s in ss.starters}
    # wr3 faces LAC->KC (tough), wr2 faces CIN->BAL (favorable) — covered → graded.
    assert by["wr3"].grade == "tough" and by["wr3"].opponent == "KC"
    assert by["wr2"].grade == "favorable" and by["wr2"].opponent == "BAL"
    # QB/K/DEF are seated but carry NO grade/opponent (uncovered).
    for name in ("qb", "k", "def"):
        assert by[name].grade is None and by[name].opponent is None


def test_flex_holds_a_flex_eligible_player_the_optimizer_seated():
    team, values = _team(_BASE)
    ss = build_start_sit(team, values, _def_grades([]), _NFL_OPP)
    flex = [s for s in ss.starters if s.slot == "FLEX"]
    assert len(flex) == 1
    assert flex[0].position in ("RB", "WR", "TE")     # flex-eligible, real seated player
    assert flex[0].player_id                          # not an empty placeholder


def test_panel_reconciles_with_h2h_ppw():
    # The panel's summed forward_ppg must equal lineup_strength_ppg (the H2H proj-pts/wk),
    # because both read the SAME optimal_lineup.
    from backend.services.matchup.startsit import available_lineup_roster
    from backend.services.trade.lineup import DEFAULT_LINEUP_RULES, lineup_strength_ppg

    team, values = _team(_BASE)
    ss = build_start_sit(team, values, _def_grades([]), _NFL_OPP)
    panel_total = round(sum(s.forward_ppg for s in ss.starters), 2)
    my_ppw = lineup_strength_ppg(available_lineup_roster(team, values), DEFAULT_LINEUP_RULES)
    assert panel_total == round(my_ppw, 2)


def test_out_and_ir_excluded_from_optimal_and_replacement_shown():
    players = list(_BASE)
    players[3] = ("wr1", "WR", 16, "MIA", "O")      # top WR is OUT
    team, values = _team(players)
    ss = build_start_sit(team, values, _def_grades([]), _NFL_OPP)
    starter_names = {s.name for s in ss.starters}
    assert "wr1" not in starter_names                # Out excluded from the lineup
    assert "wr_bench" in starter_names               # next-best WR fills in
    rep = [r for r in ss.replacements if r.out_name == "wr1"]
    assert rep and rep[0].out_status == "O" and rep[0].in_name == "wr_bench"


def test_available_roster_excludes_out_ir():
    players = list(_BASE)
    players[4] = ("wr2", "WR", 13, "CIN", "IR")
    team, values = _team(players)
    avail = available_lineup_roster(team, values)
    assert "wr2" not in {lp.player_id for lp in avail}


def test_questionable_is_flagged_not_excluded():
    players = list(_BASE)
    players[3] = ("wr1", "WR", 16, "MIA", "Q")      # top WR is Questionable
    team, values = _team(players)
    ss = build_start_sit(team, values, _def_grades([]), _NFL_OPP)
    by = {s.name: s for s in ss.starters}
    assert "wr1" in by                               # NOT excluded (may play)
    assert by["wr1"].injury_flag == "Q"              # but flagged


def test_missing_team_is_bye_no_fabricated_grade():
    players = list(_BASE)
    players[3] = ("wr1", "WR", 16, None, None)      # no NFL team
    team, values = _team(players)
    grades = _def_grades([("KC", "WR", "tough", 30)])
    ss = build_start_sit(team, values, grades, _NFL_OPP)
    wr1 = next(s for s in ss.starters if s.name == "wr1")
    assert wr1.opponent is None and wr1.grade is None   # BYE/na — no guess


def test_bench_swap_fires_only_when_founded():
    # wr_bench (10.5, GB->CHI) vs weakest WR starter wr3 (11, LAC->KC). Make CHI
    # favorable and KC tough → materially softer AND within 2ppg → swap fires.
    team, values = _team(_BASE)
    grades = _def_grades([("KC", "WR", "tough", 31), ("CHI", "WR", "favorable", 1)])
    ss = build_start_sit(team, values, grades, _NFL_OPP)
    wr_swaps = [w for w in ss.swaps if w.position == "WR"]
    assert wr_swaps and wr_swaps[0].bench_name == "wr_bench"
    assert wr_swaps[0].bench_grade == "favorable" and wr_swaps[0].starter_grade == "tough"


def test_no_swap_when_not_materially_softer():
    # Same value proximity, but CHI only neutral vs KC tough is one tier — that DOES
    # fire; here make CHI tough too → NOT softer → no swap (silence is correct).
    team, values = _team(_BASE)
    grades = _def_grades([("KC", "WR", "tough", 31), ("CHI", "WR", "tough", 30)])
    ss = build_start_sit(team, values, grades, _NFL_OPP)
    assert not [w for w in ss.swaps if w.position == "WR"]


def test_no_swap_when_bench_clearly_worse_on_value():
    # A cheap bench WR with a great matchup must NOT surface (value gate).
    players = list(_BASE) + [("wr_scrub", "WR", 3.0, "GB", None)]
    team, values = _team(players)
    grades = _def_grades([("CHI", "WR", "favorable", 1), ("KC", "WR", "tough", 31)])
    ss = build_start_sit(team, values, grades, _NFL_OPP)
    assert not [w for w in ss.swaps if w.bench_name == "wr_scrub"]


# --- as-of-week point-in-time filter (the look-ahead guard) ---
def test_as_of_week_grade_uses_only_prior_weeks():
    from backend.services.matchup import def_grades as dg

    weekly = pd.DataFrame({
        "player_id": ["a", "a", "a"], "recent_team": ["SF", "SF", "SF"],
        "week": [12, 13, 14], "fantasy_points_ppr": [20.0, 20.0, 99.0],  # wk14 is the leak
    })
    rosters = pd.DataFrame({"player_id": ["a"], "position": ["WR"]})
    sched = pd.DataFrame({"home_team": ["SF", "SF", "SF"], "away_team": ["SEA", "SEA", "SEA"],
                          "week": [12, 13, 14], "game_type": ["REG", "REG", "REG"]})
    with patch("backend.integrations.nfl_data.fetch_seasonal_rosters", return_value=rosters), \
         patch("backend.integrations.nfl_weekly.compute_weekly_pbp", return_value=weekly), \
         patch("backend.integrations.nfl_data.fetch_schedules", return_value=sched):
        frame = dg.build_weekly_ppr_by_defense(2025, 14)
    assert set(frame["week"]) == {12, 13}            # week 14 EXCLUDED — no look-ahead
    assert 99.0 not in set(frame["fantasy_points_ppr"])
