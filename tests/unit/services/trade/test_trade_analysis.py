"""
Deterministic trade-verdict tests (backend/services/trade/trade_analysis.py).

Pure: build LeagueState + InSeasonValue fixtures directly and assert the engine-
grounded verdict — lopsided reads lopsided, thin/team-change data hedges, the
winner follows forward_value (not name), and the roster guard fires only on a
net-player overflow. No DB, no LLM.
"""
from __future__ import annotations

import pytest

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend
from backend.services.trade.trade_analysis import (
    TradeValidationError,
    analyze_trade,
    validate_trade,
)


def _iv(pid, name, fv, *, conf=Confidence.FULL, trend=ValueTrend.STABLE, reason="", pos="WR"):
    return InSeasonValue(
        canonical_player_id=pid, name=name, position=pos, forward_value=fv,
        value_trend=trend, buy_low=False, sell_high=False, why="usage signal",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv / 5, expected_ppg=fv / 5, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv / 5, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=conf,
        confidence_reason=reason,
    )


def _two_team_state(my_players, opp_players):
    me = TeamState("me", "Me", True, tuple(my_players))
    opp = TeamState("opp", "Opp", False, tuple(opp_players))
    return LeagueState(2025, 14, (me, opp))


# ---------------------------------------------------------------------------
# Verdict direction / magnitude
# ---------------------------------------------------------------------------
def test_lopsided_trade_reads_lopsided():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Get", "WR")])
    values = {"g": _iv("g", "Give", 20), "x": _iv("x", "Get", 90)}
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    assert a.value_delta == 70.0
    assert a.winner == "you"
    assert a.fairness == "lopsided you"
    assert a.hedged is False


def test_even_trade_reads_fair():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Get", "WR")])
    values = {"g": _iv("g", "Give", 50), "x": _iv("x", "Get", 52)}
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    assert a.winner == "even"
    assert a.fairness == "fair"


# ---------------------------------------------------------------------------
# Confidence hedging — thin / team-change data never reads "lopsided"
# ---------------------------------------------------------------------------
def test_team_change_player_hedges_a_would_be_blowout():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Cooks", "WR")])
    values = {
        "g": _iv("g", "Give", 20),
        "x": _iv("x", "Cooks", 90, conf=Confidence.LIMITED,
                 reason="team change within last-5 window — cross-team share denominator"),
    }
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    # delta of +70 would be lopsided, but the team-change player forces a hedge.
    assert a.fairness == "lean you"        # downgraded from lopsided
    assert a.hedged is True
    assert a.confidence == "limited"
    assert "Cooks" in a.hedge_reason and "team change" in a.hedge_reason


def test_insufficient_player_hedges():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Sparse", "WR")])
    values = {
        "g": _iv("g", "Give", 20),
        "x": _iv("x", "Sparse", 90, conf=Confidence.INSUFFICIENT, reason="only 1 played game(s) — no trend"),
    }
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    assert a.hedged is True
    assert a.fairness == "lean you"
    assert a.confidence == "insufficient"


# ---------------------------------------------------------------------------
# Name-bias — verdict follows forward_value, never the name
# ---------------------------------------------------------------------------
def test_verdict_follows_engine_value_not_reputation():
    """A famous name with LOW engine value loses to an unknown with HIGH value."""
    state = _two_team_state(
        [RosterPlayer("g", "Superstar Famous", "WR")],
        [RosterPlayer("x", "Anonymous Scrub", "WR")],
    )
    values = {
        "g": _iv("g", "Superstar Famous", 80),   # big name, low forward_value? no — high fv given away
        "x": _iv("x", "Anonymous Scrub", 30),
    }
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    # I gave away the higher engine value → I lose, regardless of the names.
    assert a.value_delta == -50.0
    assert a.winner == "opponent"


# ---------------------------------------------------------------------------
# Roster guard — the one locked rule
# ---------------------------------------------------------------------------
def test_roster_guard_fires_on_net_player_overflow_with_drop_rec():
    my = [RosterPlayer("a", "A", "WR"), RosterPlayer("b", "B", "WR"), RosterPlayer("c", "C", "WR")]
    opp = [RosterPlayer("x", "X", "WR"), RosterPlayer("y", "Y", "WR")]
    state = _two_team_state(my, opp)
    values = {
        "a": _iv("a", "A", 50), "b": _iv("b", "B", 10), "c": _iv("c", "C", 90),
        "x": _iv("x", "X", 40), "y": _iv("y", "Y", 30),
    }
    # 2-for-1 at a 3-slot limit → 4 players, over by 1.
    a = analyze_trade(state, values, "me", ["a"], ["x", "y"], roster_limit=3)
    g = a.roster_guard
    assert g.triggered is True
    assert g.net_players == 1
    # Lowest-value keeper (B, fv 10; A is being traded) is the drop rec.
    assert [r["name"] for r in g.drop_recommendations] == ["B"]


def test_roster_guard_silent_on_balanced_swap():
    my = [RosterPlayer("a", "A", "WR"), RosterPlayer("b", "B", "WR"), RosterPlayer("c", "C", "WR")]
    opp = [RosterPlayer("x", "X", "WR")]
    state = _two_team_state(my, opp)
    values = {"a": _iv("a", "A", 50), "b": _iv("b", "B", 10), "c": _iv("c", "C", 90), "x": _iv("x", "X", 40)}
    a = analyze_trade(state, values, "me", ["a"], ["x"], roster_limit=3)
    assert a.roster_guard.triggered is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_validate_rejects_unknown_team_and_misplaced_players():
    state = _two_team_state([RosterPlayer("g", "G", "WR")], [RosterPlayer("x", "X", "WR")])
    values = {"g": _iv("g", "G", 50), "x": _iv("x", "X", 50)}
    with pytest.raises(TradeValidationError):
        validate_trade(state, values, "nope", ["g"], ["x"])     # unknown team
    with pytest.raises(TradeValidationError):
        validate_trade(state, values, "me", ["x"], ["g"])       # give not on my team
    with pytest.raises(TradeValidationError):
        validate_trade(state, values, "me", ["g"], [])          # empty side


# ---------------------------------------------------------------------------
# Empty-slot WARNING (#179 follow-up) — a surfaced position-punt discloses the
# emptied required starter slot (a heads-up; the value math still credits the
# streamable replacement, so the trade is fine — the user just must be told).
# ---------------------------------------------------------------------------
def _full_roster_state():
    """Acquirer with a full 1QB/2RB/3WR/1TE lineup (8 players) + an opponent with
    a WR/TE/QB to acquire, so a trade can empty a required slot."""
    my = [RosterPlayer(p, p, pos) for p, pos in [
        ("qb", "QB"), ("rb1", "RB"), ("rb2", "RB"), ("rb3", "RB"),
        ("wr1", "WR"), ("wr2", "WR"), ("wr3", "WR"), ("te", "TE")]]
    opp = [RosterPlayer(p, p, pos) for p, pos in [
        ("owr", "WR"), ("owr2", "WR"), ("ote", "TE"), ("oqb", "QB")]]
    state = _two_team_state(my, opp)
    specs = [("qb", "QB", 30), ("rb1", "RB", 40), ("rb2", "RB", 35), ("rb3", "RB", 20),
             ("wr1", "WR", 38), ("wr2", "WR", 34), ("wr3", "WR", 30), ("te", "TE", 25),
             ("owr", "WR", 45), ("owr2", "WR", 44), ("ote", "TE", 22), ("oqb", "QB", 28)]
    values = {p: _iv(p, p, fv, pos=pos) for p, pos, fv in specs}
    return state, values


def test_emptying_only_te_fires_warning_with_position():
    state, values = _full_roster_state()
    a = analyze_trade(state, values, "me", ["te"], ["owr"], roster_limit=16)  # 0 TE after
    assert len(a.warnings) == 1
    w = a.warnings[0]
    assert w.type == "empty_required_slot" and w.position == "TE"
    assert "only TE" in w.message


def test_filled_slot_trade_has_no_warning():
    state, values = _full_roster_state()
    a = analyze_trade(state, values, "me", ["wr3"], ["owr"], roster_limit=16)  # keeps all slots
    assert a.warnings == ()


def test_emptying_two_required_slots_fires_two_warnings():
    state, values = _full_roster_state()
    a = analyze_trade(state, values, "me", ["qb", "te"], ["owr", "owr2"], roster_limit=16)
    positions = {w.position for w in a.warnings}
    assert positions == {"QB", "TE"}
    assert all(w.type == "empty_required_slot" for w in a.warnings)


def test_unfilled_flex_is_not_flagged():
    # A 7-player roster fills all REQUIRED slots (1QB/2RB/3WR/1TE) but leaves the
    # FLEX empty. A trade that keeps every required slot filled → NO warning
    # (FLEX is flexible; only fixed required slots count).
    my = [RosterPlayer(p, p, pos) for p, pos in [
        ("qb", "QB"), ("rb1", "RB"), ("rb2", "RB"),
        ("wr1", "WR"), ("wr2", "WR"), ("wr3", "WR"), ("te", "TE")]]
    opp = [RosterPlayer("owr", "owr", "WR")]
    state = _two_team_state(my, opp)
    specs = [("qb", "QB", 30), ("rb1", "RB", 40), ("rb2", "RB", 35),
             ("wr1", "WR", 38), ("wr2", "WR", 34), ("wr3", "WR", 30), ("te", "TE", 25),
             ("owr", "WR", 45)]
    values = {p: _iv(p, p, fv, pos=pos) for p, pos, fv in specs}
    a = analyze_trade(state, values, "me", ["wr3"], ["owr"], roster_limit=16)  # FLEX stays empty
    assert a.warnings == ()          # unfilled FLEX is NOT a warning
