"""
Edge-band gate tests (slice 4, trade_acceptability_design.md §3). The headline:
the gate REJECTS the zero-sum robbery the old winner=="you" bar surfaced, and
SURFACES genuine positive-sum trades — judged in contextual value against BOTH
rosters, with the overtake guard as condition 4.
"""
from __future__ import annotations

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.lineup import LineupPlayer
from backend.services.trade.trade_proposals import (
    Candidate,
    _COMFORT_THRESHOLD,
    enumerate_candidates,
    evaluate_candidates,
    evaluate_edge_band,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _lp(spec):
    return [LineupPlayer(pid, pos, fv) for pid, pos, fv in spec]


# Roster shapes (pid, pos, forward_value). Me: RB-rich / WR-thin (strong);
# Them: WR-rich / RB-thin. The slice-2 asymmetry that makes positive-sum exist.
ME_STRONG = [("qm", "QB", 22), ("rm1", "RB", 24), ("rm2", "RB", 22), ("rm3", "RB", 20),
             ("rm4", "RB", 15), ("rm5", "RB", 13),  # rm4/rm5: surplus RBs (below my FLEX)
             ("wm1", "WR", 16), ("wm2", "WR", 14), ("tm", "TE", 15)]
THEM = [("qt", "QB", 19), ("rt1", "RB", 9), ("btr", "RB", 7),
        ("wt1", "WR", 20), ("wt2", "WR", 18), ("wt3", "WR", 16), ("wt4", "WR", 14),
        ("wt5", "WR", 13), ("wt6", "WR", 12),  # wt5/wt6: surplus WRs (benched)
        ("tt", "TE", 14)]


# ---------------------------------------------------------------------------
# evaluate_edge_band — the four conditions
# ---------------------------------------------------------------------------
def test_mutual_benefit_trade_clears_all_four_conditions():
    # Give my surplus RB (rm4, my 4th) for their surplus WR (wt5, benched): I'm
    # WR-thin so wt5 starts for me; they're RB-thin so rm4 starts for them.
    e = evaluate_edge_band(_lp(ME_STRONG), _lp(THEM), ["rm4"], ["wt5"])
    assert e.clears is True
    assert e.your_net > 0                      # 1: I improve
    assert e.their_net > _COMFORT_THRESHOLD    # 2: they improve comfortably
    assert e.your_net > e.their_net            # 3: I keep the edge
    assert e.my_strength >= e.their_strength   # 4: I stay stronger on the field


def test_robbery_is_rejected_by_condition_2():
    # The Najee-for-Taylor class: I give a scrub, get their stud → huge your_net,
    # but they LOSE — the OLD winner=="you" bar would have surfaced it.
    me = _lp([("q", "QB", 22), ("r1", "RB", 20), ("r2", "RB", 18),
              ("w1", "WR", 16), ("w2", "WR", 14), ("w3", "WR", 12),
              ("t", "TE", 13), ("scrub", "WR", 5)])
    them = _lp([("q2", "QB", 19), ("stud", "RB", 24), ("r9", "RB", 9),
                ("w4", "WR", 15), ("w5", "WR", 13), ("w6", "WR", 11), ("t2", "TE", 10)])
    e = evaluate_edge_band(me, them, ["scrub"], ["stud"])
    assert e.your_net > 0          # I'd clearly win (old bar would surface this)
    assert e.their_net < 0         # but they lose — fails condition 2
    assert e.clears is False


def test_perspective_is_per_roster_not_zero_sum():
    # In a positive-sum trade BOTH nets are positive — impossible under the old
    # intrinsic/zero-sum value (where their_net would be exactly −your_net).
    e = evaluate_edge_band(_lp(ME_STRONG), _lp(THEM), ["rm4"], ["wt5"])
    assert e.your_net > 0 and e.their_net > 0
    assert e.their_net != -e.your_net


def test_condition_4_blocks_a_trade_that_passes_1_through_3():
    # Same beneficial swap, but my roster is weak everywhere except the RB I give
    # → I'm behind on the field, and the trade (good for me 1-3) still leaves me
    # behind → condition 4 fails.
    me_weak = _lp([("qm", "QB", 8), ("rm1", "RB", 24), ("rm2", "RB", 22),
                   ("rm3", "RB", 20), ("rm4", "RB", 15),
                   ("wm1", "WR", 6), ("wm2", "WR", 5), ("tm", "TE", 6)])
    e = evaluate_edge_band(me_weak, _lp(THEM), ["rm4"], ["wt5"])
    assert e.your_net > 0 and e.their_net > _COMFORT_THRESHOLD and e.your_net > e.their_net
    assert e.my_strength < e.their_strength    # I stay behind
    assert e.clears is False                   # condition 4 blocks it


# ---------------------------------------------------------------------------
# evaluate_candidates — surfacing, never-pad, ranking (LeagueState path)
# ---------------------------------------------------------------------------
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


def _league(my_spec, opp_spec):
    me = TeamState("me", "Me", True,
                   tuple(RosterPlayer(pid, f"P-{pid}", pos) for pid, pos, _ in my_spec))
    opp = TeamState("opp", "Opp", False,
                    tuple(RosterPlayer(pid, f"P-{pid}", pos) for pid, pos, _ in opp_spec))
    state = LeagueState(2025, 14, (me, opp))
    values = {pid: _iv(pid, pos, fv) for pid, pos, fv in (*my_spec, *opp_spec)}
    return state, values


def test_evaluate_candidates_surfaces_the_mutual_benefit_trade():
    state, values = _league(ME_STRONG, THEM)
    cand = Candidate(("rm4",), ("wt5",), "opp")
    out = evaluate_candidates(state, values, "me", [cand], roster_limit=16)
    assert len(out) == 1
    _, _, edge = out[0]
    assert edge.clears and edge.your_net > 0 and edge.their_net > _COMFORT_THRESHOLD


def test_never_pads_when_nothing_clears():
    # I'm strictly stronger at every position → no swap helps both sides.
    my_spec = [("q", "QB", 25), ("r1", "RB", 24), ("r2", "RB", 22),
               ("w1", "WR", 20), ("w2", "WR", 18), ("w3", "WR", 16),
               ("t", "TE", 14), ("br", "RB", 13), ("bw", "WR", 12)]
    opp_spec = [("oq", "QB", 10), ("or1", "RB", 9), ("or2", "RB", 7),
                ("ow1", "WR", 8), ("ow2", "WR", 6), ("ow3", "WR", 5),
                ("ot", "TE", 4), ("obw", "WR", 3)]
    state, values = _league(my_spec, opp_spec)
    out = evaluate_candidates(state, values, "me",
                              enumerate_candidates(state, values, "me"), roster_limit=16)
    assert out == []        # first-class empty — not padded/loosened


def test_cleared_candidates_ranked_by_your_net_descending():
    state, values = _league(ME_STRONG, THEM)
    # Three beneficial swaps of surplus RBs for surplus WRs, distinct your_net.
    cands = [Candidate(("rm4",), ("wt5",), "opp"),
             Candidate(("rm4",), ("wt6",), "opp"),
             Candidate(("rm5",), ("wt6",), "opp")]
    out = evaluate_candidates(state, values, "me", cands, roster_limit=16)
    assert len(out) >= 2
    nets = [edge.your_net for _, _, edge in out]
    assert nets == sorted(nets, reverse=True)    # highest your_net first


def test_cap_at_five():
    state, values = _league(ME_STRONG, THEM)
    # Many duplicate-ish candidates; even if all cleared, never more than 5.
    cands = [Candidate(("rm4",), (w,), "opp") for w in ("wt5", "wt4", "wt3", "wt2", "wt1")]
    out = evaluate_candidates(state, values, "me", cands * 3, roster_limit=16)
    assert len(out) <= 5


def test_enumerate_candidates_is_now_targeted_need_surplus():
    # Slice 6 replaced exhaustive 1-for-1 with need/surplus targeting: my surplus
    # RBs (rm4/rm5) ↔ their need (RB); their surplus WRs (wt5/wt6) ↔ my need (WR).
    # give-pool {rm4,rm5} × get-pool {wt5,wt6}, shapes 1-2 per side = 3×3 = 9.
    state, values = _league(ME_STRONG, THEM)
    cands = enumerate_candidates(state, values, "me")
    assert len(cands) == 9
    assert all(c.counterparty_team_id == "opp" for c in cands)
    # only the matched surplus pieces appear — never their starters.
    assert {p for c in cands for p in c.give_ids} == {"rm4", "rm5"}
    assert {p for c in cands for p in c.get_ids} == {"wt5", "wt6"}
