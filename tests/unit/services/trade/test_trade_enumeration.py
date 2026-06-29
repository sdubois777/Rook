"""
Targeted multi-player ENUMERATION tests (slice 6, trade_acceptability_design.md
§6d/§6e). This slice changes only WHICH candidates get generated — need/surplus
targeting + bounded multi-player shapes — never how they're judged. The slice-4
edge-band gate + rank-by-your_net are reused UNCHANGED, so every test that asserts
"surfaces" / "doesn't clear" exercises the SAME gate.

The headline: a multi-player consolidation the old exhaustive 1-for-1 enumeration
COULD NOT produce now gets generated, clears the gate, and surfaces.

NOTE on shape (a real finding, documented in the report): under the strict
four-condition gate, the clearing multi-player consolidation is BALANCED (2-for-2)
— a give-2-get-1 cannot clear because the help screen only proposes give-pieces
that materially help the opponent, so a 2-for-1 hands them two starters and
condition 3 (you keep the edge) fails. That is correct: you'd be giving more than
you get. The enumeration still GENERATES every shape 1-to-3 per side (incl. 2-for-1
and uneven) and the gate judges them identically.
"""
from __future__ import annotations

from math import comb

import pytest

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_analysis import analyze_trade
from backend.services.trade.trade_proposals import (
    Candidate,
    _can_fit,
    analyze_roster,
    enumerate_candidates,
    evaluate_candidates,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _iv(pid, pos, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=f"P-{pid}", position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv / 5, expected_ppg=fv / 5, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv / 5, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _team(team_id, name, is_me, spec):
    return TeamState(team_id, name, is_me,
                     tuple(RosterPlayer(pid, f"P-{pid}", pos) for pid, pos, _ in spec))


def _league(my_spec, opp_spec):
    state = LeagueState(2025, 14, (_team("me", "Me", True, my_spec),
                                   _team("opp", "Opp", False, opp_spec)))
    values = {pid: _iv(pid, pos, fv) for pid, pos, fv in (*my_spec, *opp_spec)}
    return state, values


# Me RB-rich / WR-thin; them WR-rich / RB-thin — the asymmetry positive-sum needs.
ME_STRONG = [("qm", "QB", 22), ("rm1", "RB", 24), ("rm2", "RB", 22), ("rm3", "RB", 20),
             ("rm4", "RB", 15), ("rm5", "RB", 13), ("wm1", "WR", 16), ("wm2", "WR", 14),
             ("tm", "TE", 15)]
THEM = [("qt", "QB", 19), ("rt1", "RB", 9), ("btr", "RB", 7),
        ("wt1", "WR", 20), ("wt2", "WR", 18), ("wt3", "WR", 16), ("wt4", "WR", 14),
        ("wt5", "WR", 13), ("wt6", "WR", 12), ("tt", "TE", 14)]


# ---------------------------------------------------------------------------
# BUILD 1 — need / surplus analysis
# ---------------------------------------------------------------------------
def test_need_surplus_identifies_rb_surplus_and_wr_need():
    # 4 startable RBs (only 3 can start: 2 RB slots + FLEX) + 1 weak WR (2 WR
    # slots empty) → the 4th RB is surplus, WR is a need, RB is NOT.
    spec = [("qm", "QB", 18), ("r1", "RB", 20), ("r2", "RB", 19), ("r3", "RB", 18),
            ("r4", "RB", 17), ("wweak", "WR", 4), ("tm", "TE", 12)]
    state, values = _league(spec, [("ox", "RB", 10)])
    a = analyze_roster(state.my_team, values)
    assert a.surplus_ids == ("r4",)          # the startable 4th RB rides the bench
    assert "WR" in a.needs                    # thin/weak at WR
    assert "RB" not in a.needs                # RB is a strength, not a need


# ---------------------------------------------------------------------------
# BUILD 2 — the HEADLINE: a multi-player consolidation impossible under 1-for-1
# ---------------------------------------------------------------------------
def test_multiplayer_consolidation_is_generated_and_surfaces():
    state, values = _league(ME_STRONG, THEM)
    cands = enumerate_candidates(state, values, "me")

    consolidation = Candidate(("rm4", "rm5"), ("wt5", "wt6"), "opp")
    assert consolidation in cands             # GENERATED (1-for-1 never could)

    surfaced = evaluate_candidates(state, values, "me", cands, roster_limit=16)
    shapes = {(c.give_ids, c.get_ids) for c, _, _ in surfaced}
    assert (("rm4", "rm5"), ("wt5", "wt6")) in shapes   # CLEARS the gate + surfaces
    # it is genuinely multi-player on both sides — no 1-for-1 produces it.
    multi = [(c, e) for c, _, e in surfaced
             if len(c.give_ids) == 2 and len(c.get_ids) == 2]
    assert multi and all(e.your_net > 0 and e.their_net > 0 for _, e in multi)


def test_enumeration_targets_matched_surplus_for_need_only():
    state, values = _league(ME_STRONG, THEM)
    cands = enumerate_candidates(state, values, "me")
    # only my surplus RBs are ever given; only their surplus WRs are ever gotten —
    # starters on neither side are touched (that's the targeting / pruning).
    assert {p for c in cands for p in c.give_ids} == {"rm4", "rm5"}
    assert {p for c in cands for p in c.get_ids} == {"wt5", "wt6"}


# ---------------------------------------------------------------------------
# CAP AT 3 — never 4+ on a side
# ---------------------------------------------------------------------------
def test_no_candidate_exceeds_three_players_per_side():
    many_me = [("qm", "QB", 20), ("r1", "RB", 22), ("r2", "RB", 20),
               ("rA", "RB", 15), ("rB", "RB", 14), ("rC", "RB", 13), ("rD", "RB", 12),
               ("rE", "RB", 11), ("w1", "WR", 8), ("tm", "TE", 6)]
    many_them = [("qt", "QB", 19), ("wt1", "WR", 22), ("wt2", "WR", 20),
                 ("wA", "WR", 15), ("wB", "WR", 14), ("wC", "WR", 13), ("wD", "WR", 12),
                 ("wE", "WR", 11), ("rt1", "RB", 7), ("tt", "TE", 6)]
    state, values = _league(many_me, many_them)
    cands = enumerate_candidates(state, values, "me")
    assert cands                                              # plenty to combine
    assert all(len(c.give_ids) <= 3 and len(c.get_ids) <= 3 for c in cands)
    # the cap actually BINDS here (3-piece sides exist) — not vacuously satisfied.
    assert any(len(c.give_ids) == 3 for c in cands)
    assert any(len(c.get_ids) == 3 for c in cands)


# ---------------------------------------------------------------------------
# EFFICIENCY — targeting prunes orders of magnitude below all-subsets brute force
# ---------------------------------------------------------------------------
def test_targeted_enumeration_is_bounded_vs_brute_force():
    state, values = _league(ME_STRONG, THEM)
    cands = enumerate_candidates(state, values, "me")

    # all-subsets brute force (1..3 per side, every opponent) for the same league.
    my_n = len(state.my_team.roster)
    brute = 0
    for opp in state.teams:
        if opp.team_id == "me":
            continue
        o = len(opp.roster)
        brute += sum(comb(my_n, k) for k in (1, 2, 3)) * sum(comb(o, k) for k in (1, 2, 3))

    assert len(cands) * 100 < brute        # orders of magnitude below brute force
    assert len(cands) <= 50                # absolute guard against re-explosion


# ---------------------------------------------------------------------------
# BUILD 3 — roster-slot legality on uneven trades
# ---------------------------------------------------------------------------
def test_can_fit_legality_rules():
    # Receiving fewer than you give (or equal) is always legal.
    assert _can_fit(size=15, out_n=2, in_n=1, limit=16) is True
    assert _can_fit(size=15, out_n=1, in_n=1, limit=16) is True
    # Receiving more, with room: legal.
    assert _can_fit(size=14, out_n=1, in_n=2, limit=16) is True
    # Receiving more, no open slot but a drop absorbs it: legal.
    assert _can_fit(size=16, out_n=1, in_n=2, limit=16) is True
    # Impossible even dropping everything kept (receive more than the whole limit).
    assert _can_fit(size=1, out_n=0, in_n=2, limit=1) is False


def test_uneven_trade_slot_guard_flags_drop_or_passes():
    # roster_limit == my roster size (9) so any net-positive trade overfills.
    state, values = _league(ME_STRONG, THEM)
    # 1-for-2 (give rm4, get wt5+wt6) → net +1 → overfills → flagged with a drop.
    over = analyze_trade(state, values, "me", ["rm4"], ["wt5", "wt6"], roster_limit=9)
    assert over.roster_guard.triggered is True
    assert over.roster_guard.net_players == 1
    assert over.roster_guard.drop_recommendations            # lowest-value droppable
    # 2-for-1 (give rm4+rm5, get wt5) → net -1 → always slot-legal, no flag.
    under = analyze_trade(state, values, "me", ["rm4", "rm5"], ["wt5"], roster_limit=9)
    assert under.roster_guard.triggered is False


# ---------------------------------------------------------------------------
# GATE UNCHANGED — a multi-player robbery still fails condition 2; never-pad holds
# ---------------------------------------------------------------------------
def test_multiplayer_robbery_still_fails_the_gate():
    # Give two scrubs, get their stud: huge for me, but they lose value → the
    # SAME condition-2 that kills the 1-for-1 robbery kills the 2-for-1 one.
    rob_me = [("qm", "QB", 20), ("r1", "RB", 22), ("r2", "RB", 20), ("w1", "WR", 18),
              ("w2", "WR", 16), ("w3", "WR", 14), ("tm", "TE", 13),
              ("s1", "WR", 4), ("s2", "WR", 3)]
    rob_them = [("qt", "QB", 18), ("stud", "RB", 24), ("r9", "RB", 9), ("w4", "WR", 15),
                ("w5", "WR", 13), ("w6", "WR", 11), ("tt", "TE", 10)]
    state, values = _league(rob_me, rob_them)
    surfaced = evaluate_candidates(
        state, values, "me", [Candidate(("s1", "s2"), ("stud",), "opp")], roster_limit=16,
    )
    assert surfaced == []        # rejected — they'd never accept losing their stud


def test_never_pads_when_no_targeted_candidate_clears():
    # Strictly-dominant me: every player starts (no surplus) → nothing to target →
    # enumeration is empty and the surfaced set is a first-class empty.
    dom_me = [("q", "QB", 25), ("r1", "RB", 24), ("r2", "RB", 22), ("w1", "WR", 20),
              ("w2", "WR", 18), ("w3", "WR", 16), ("t", "TE", 14), ("br", "RB", 13)]
    weak_them = [("oq", "QB", 10), ("or1", "RB", 9), ("or2", "RB", 7), ("ow1", "WR", 8),
                 ("ow2", "WR", 6), ("ow3", "WR", 5), ("ot", "TE", 4)]
    state, values = _league(dom_me, weak_them)
    cands = enumerate_candidates(state, values, "me")
    assert evaluate_candidates(state, values, "me", cands, roster_limit=16) == []
