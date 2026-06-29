"""
Pure tests for the proposals filter (backend/services/trade/trade_proposals.py).

The never-pad + cap + rank guarantees are deterministic Python (not the LLM), so
they're proven here directly on hand-built candidate sets run through slice-3's
verdict — including the headline never-pad case where padding to a count would be
the tempting wrong answer.
"""
from __future__ import annotations

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend
from backend.services.trade.trade_proposals import (
    Candidate,
    enumerate_candidates,
    evaluate_candidates,
)


def _iv(pid, name, fv, *, conf=Confidence.FULL, reason=""):
    return InSeasonValue(
        canonical_player_id=pid, name=name, position="WR", forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="usage",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv / 5, expected_ppg=fv / 5, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv / 5, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=conf,
        confidence_reason=reason,
    )


def _league(my_players, opp_players):
    me = TeamState("me", "Me", True, tuple(RosterPlayer(p, p.upper(), "WR") for p in my_players))
    opp = TeamState("opp", "Opp", False, tuple(RosterPlayer(p, p.upper(), "WR") for p in opp_players))
    return LeagueState(2025, 14, (me, opp))


def _cand(give, get):
    return Candidate((give,), (get,), "opp")


# ---------------------------------------------------------------------------
# NEVER-PAD — the headline test
# ---------------------------------------------------------------------------
def test_never_pads_to_a_count_when_only_one_trade_is_good():
    """My low player vs five opponents: only ONE swap actually gains me value;
    the rest are even or losing. Padding to 3-5 would be the tempting wrong
    answer — prove exactly ONE surfaces."""
    state = _league(["g"], ["x", "e1", "e2", "e3", "w1"])
    values = {
        "g": _iv("g", "G", 50),
        "x": _iv("x", "X", 90),    # clear upgrade → surfaces
        "e1": _iv("e1", "E1", 50), # even → no
        "e2": _iv("e2", "E2", 52), # within fair band → no
        "e3": _iv("e3", "E3", 30), # losing → no
        "w1": _iv("w1", "W1", 20), # losing → no
    }
    candidates = [_cand("g", o) for o in ("x", "e1", "e2", "e3", "w1")]
    surfaced = evaluate_candidates(state, values, "me", candidates, roster_limit=16)
    assert len(surfaced) == 1
    assert surfaced[0][0].get_ids == ("x",)
    assert surfaced[0][1].winner == "you"


def test_returns_empty_when_no_trade_clears_the_bar():
    state = _league(["g"], ["a", "b"])
    values = {"g": _iv("g", "G", 80), "a": _iv("a", "A", 30), "b": _iv("b", "B", 40)}
    surfaced = evaluate_candidates(
        state, values, "me", [_cand("g", "a"), _cand("g", "b")], roster_limit=16,
    )
    assert surfaced == []   # → route turns this into "no clear trade right now"


# ---------------------------------------------------------------------------
# CAP + RANK
# ---------------------------------------------------------------------------
def test_caps_at_five_even_when_more_clear():
    opps = [f"o{i}" for i in range(8)]
    state = _league(["g"], opps)
    values = {"g": _iv("g", "G", 10)}
    values.update({o: _iv(o, o.upper(), 90 - i) for i, o in enumerate(opps)})  # all >> g
    surfaced = evaluate_candidates(
        state, values, "me", [_cand("g", o) for o in opps], roster_limit=16,
    )
    assert len(surfaced) == 5                      # capped
    deltas = [a.value_delta for _, a in surfaced]
    assert deltas == sorted(deltas, reverse=True)  # ranked by user gain


# ---------------------------------------------------------------------------
# HEDGING — a thin-data winner surfaces hedged, not as a blowout
# ---------------------------------------------------------------------------
def test_team_change_winner_surfaces_but_hedged():
    state = _league(["g"], ["x"])
    values = {
        "g": _iv("g", "G", 20),
        "x": _iv("x", "Cooks", 90, conf=Confidence.LIMITED,
                 reason="team change within last-5 window — cross-team share denominator"),
    }
    surfaced = evaluate_candidates(state, values, "me", [_cand("g", "x")], roster_limit=16)
    assert len(surfaced) == 1
    analysis = surfaced[0][1]
    assert analysis.winner == "you"          # clears the bar
    assert analysis.hedged is True
    assert analysis.fairness == "lean you"   # NOT lopsided — hedged


# ---------------------------------------------------------------------------
# enumerate fallback
# ---------------------------------------------------------------------------
def test_enumerate_candidates_is_all_one_for_one_across_opponents():
    state = _league(["g1", "g2"], ["x1", "x2", "x3"])
    cands = enumerate_candidates(state, "me")
    assert len(cands) == 2 * 3
    assert all(len(c.give_ids) == 1 and len(c.get_ids) == 1 for c in cands)
    assert all(c.counterparty_team_id == "opp" for c in cands)
