"""
Tests for optimal_lineup (backend/services/trade/lineup.py) — slice 1 of the
trade acceptability model. Proves it picks the OPTIMAL legal lineup (incl.
FLEX), respects position eligibility, handles thin rosters, and is deterministic.
"""
from __future__ import annotations

from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    LineupRules,
    optimal_lineup,
)


def _p(pid, pos, fv):
    return LineupPlayer(player_id=pid, position=pos, forward_value=fv)


def _ids(lineup):
    return {s.player_id for s in lineup.starters}


# ---------------------------------------------------------------------------
# Basic: the correct highest-value legal lineup
# ---------------------------------------------------------------------------
def test_basic_picks_highest_value_legal_lineup():
    roster = [
        _p("q1", "QB", 25),
        _p("r1", "RB", 20), _p("r2", "RB", 15), _p("r3", "RB", 8),
        _p("w1", "WR", 18), _p("w2", "WR", 16), _p("w3", "WR", 14), _p("w4", "WR", 12),
        _p("t1", "TE", 10), _p("t2", "TE", 4),
    ]
    lu = optimal_lineup(roster)
    # QB q1; RB r1,r2; WR w1,w2,w3; TE t1; FLEX best remaining = w4 (12 > r3 8 > t2 4)
    assert _ids(lu) == {"q1", "r1", "r2", "w1", "w2", "w3", "t1", "w4"}
    assert lu.strength == 25 + 20 + 15 + 18 + 16 + 14 + 10 + 12  # 130
    assert dict(lu.slots)["FLEX"] == "w4"


# ---------------------------------------------------------------------------
# FLEX correctness: best remaining RB/WR/TE, and beats the naive "stop" answer
# ---------------------------------------------------------------------------
def test_flex_takes_best_remaining_eligible_not_naive_stop():
    roster = [
        _p("q1", "QB", 20),
        _p("r1", "RB", 10), _p("r2", "RB", 9), _p("r3", "RB", 3),
        _p("w1", "WR", 8), _p("w2", "WR", 7), _p("w3", "WR", 6), _p("w4", "WR", 5),
        _p("t1", "TE", 4),
    ]
    lu = optimal_lineup(roster)
    # FLEX = the leftover WR (5) — beats the leftover RB (3).
    assert dict(lu.slots)["FLEX"] == "w4"
    assert "r3" not in _ids(lu)
    # Optimum (45 + QB/TE) includes the FLEX; naive "dedicated only" would be 5 less.
    naive_stop = 20 + 10 + 9 + 8 + 7 + 6 + 4          # no FLEX = 64
    assert lu.strength == naive_stop + 5               # 69, FLEX adds w4(5)


# ---------------------------------------------------------------------------
# Position eligibility: a WR never lands in an RB slot
# ---------------------------------------------------------------------------
def test_wr_never_fills_rb_slot():
    roster = [
        _p("q1", "QB", 20), _p("r1", "RB", 15),
        _p("w1", "WR", 18), _p("w2", "WR", 16), _p("w3", "WR", 14),
        _p("w4", "WR", 12), _p("w5", "WR", 10), _p("t1", "TE", 8),
    ]
    lu = optimal_lineup(roster)
    slots = dict(lu.slots)
    assert slots["RB1"] == "r1"
    assert slots["RB2"] is None        # only one RB — the 2nd RB slot stays EMPTY
    # FLEX takes the best leftover WR (w4), never an RB-slot filled by a WR.
    assert slots["FLEX"] == "w4"
    assert lu.strength == 20 + 15 + 18 + 16 + 14 + 8 + 12  # 103


# ---------------------------------------------------------------------------
# Degenerate: thinner than the lineup → legal partial, sane strength, no crash
# ---------------------------------------------------------------------------
def test_degenerate_roster_fills_what_is_legal():
    roster = [_p("r1", "RB", 10), _p("w1", "WR", 8)]   # 2 players, no QB/TE
    lu = optimal_lineup(roster)
    assert _ids(lu) == {"r1", "w1"}
    assert lu.strength == 18
    slots = dict(lu.slots)
    assert slots["QB"] is None and slots["RB2"] is None and slots["TE"] is None
    assert slots["FLEX"] is None       # nothing eligible left for FLEX
    assert len(lu.starters) == 2       # no crash, no phantom starters


def test_empty_roster_is_zero_strength_no_crash():
    lu = optimal_lineup([])
    assert lu.starters == () and lu.strength == 0.0
    assert all(pid is None for _, pid in lu.slots)


# ---------------------------------------------------------------------------
# Determinism: equal-value players tie-break stably by id
# ---------------------------------------------------------------------------
def test_equal_value_ties_break_by_id_and_are_stable():
    rules = LineupRules(slots={"RB": 1}, flex_count=0, flex_positions=())
    roster = [_p("b", "RB", 10), _p("a", "RB", 10)]    # same value, ids a/b
    first = optimal_lineup(roster, rules)
    second = optimal_lineup(list(reversed(roster)), rules)
    assert _ids(first) == {"a"}                         # lower id wins the tie
    assert _ids(first) == _ids(second)                  # stable regardless of input order


# ---------------------------------------------------------------------------
# Rules are parameterized (not hardcoded 1/2/3/1/1)
# ---------------------------------------------------------------------------
def test_custom_lineup_rules_are_respected():
    rules = LineupRules(slots={"QB": 2}, flex_count=0, flex_positions=())  # 2-QB league
    roster = [_p("q1", "QB", 25), _p("q2", "QB", 18), _p("q3", "QB", 10), _p("w1", "WR", 30)]
    lu = optimal_lineup(roster, rules)
    assert _ids(lu) == {"q1", "q2"}        # both QB slots, WR ineligible here
    assert lu.strength == 43
    assert DEFAULT_LINEUP_RULES.flex_positions == ("RB", "WR", "TE")
