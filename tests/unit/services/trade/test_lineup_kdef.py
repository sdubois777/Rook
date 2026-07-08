"""
K/DST lineup slots (K/DEF streaming arc, slice 3) — K and DST SEAT in their own
dedicated slots, are NOT flex-eligible, and offense-only rules are byte-identical.
"""
from __future__ import annotations

from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    LineupRules,
    lineup_rules_from_slots,
    lineup_strength_ppg,
    optimal_lineup,
)


def _p(pid, pos, fv, ppg):
    return LineupPlayer(pid, pos, fv, ppg)


def test_default_rules_have_k_and_dst_slots_not_flex():
    assert DEFAULT_LINEUP_RULES.slots.get("K") == 1
    assert DEFAULT_LINEUP_RULES.slots.get("DEF") == 1
    assert "K" not in DEFAULT_LINEUP_RULES.flex_positions
    assert "DEF" not in DEFAULT_LINEUP_RULES.flex_positions


def test_k_and_dst_seat_in_their_own_slots():
    roster = [
        _p("qb", "QB", 90, 20), _p("rb1", "RB", 80, 15), _p("rb2", "RB", 70, 12),
        _p("wr1", "WR", 85, 16), _p("wr2", "WR", 75, 13), _p("wr3", "WR", 65, 11),
        _p("te", "TE", 60, 9), _p("k", "K", 50, 9), _p("dst", "DEF", 55, 10),
    ]
    slots = dict(optimal_lineup(roster, DEFAULT_LINEUP_RULES).slots)
    assert slots["K"] == "k" and slots["DEF"] == "dst"


def test_kdst_are_not_flex_eligible():
    # Two DST + a lone RB: the best DST seats DEF; the 2nd DST is BENCHED (not
    # flexed), and the FLEX stays empty rather than taking the surplus DST.
    roster = [_p("dst1", "DEF", 90, 12), _p("dst2", "DEF", 80, 11), _p("rb1", "RB", 60, 10)]
    ol = optimal_lineup(roster, DEFAULT_LINEUP_RULES)
    slots = dict(ol.slots)
    seated = {pid for _, pid in ol.slots if pid}
    assert slots["DEF"] == "dst1"
    assert slots["FLEX"] is None          # surplus DST does NOT fill the FLEX
    assert "dst2" not in seated


def test_offense_only_rules_never_seat_kdst():
    roster = [_p("qb", "QB", 90, 20), _p("k", "K", 99, 15), _p("dst", "DEF", 99, 15)]
    off_rules = LineupRules(slots={"QB": 1}, flex_count=0, flex_positions=())
    seated = {pid for _, pid in optimal_lineup(roster, off_rules).slots if pid}
    assert seated == {"qb"}               # offense-only rules seat only offense


def test_lineup_strength_reflects_kdst_ppw():
    roster = [_p("qb", "QB", 90, 20), _p("k", "K", 50, 9), _p("dst", "DEF", 55, 10)]
    rules = LineupRules(slots={"QB": 1, "K": 1, "DEF": 1}, flex_count=0, flex_positions=())
    assert lineup_strength_ppg(roster, rules) == 39.0   # 20 + 9 + 10


def test_rules_from_slots_extracts_kdst():
    r = lineup_rules_from_slots({"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1})
    assert r.slots["K"] == 1 and r.slots["DEF"] == 1
    assert "K" not in r.flex_positions and "DEF" not in r.flex_positions
