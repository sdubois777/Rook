"""
Edge-band gate tests — now on the LINEUP-IMPROVEMENT objective
(trade_lineup_value_design.md §6). A candidate surfaces only if it IMPROVES YOUR
STARTING LINEUP (clause a) or maintains-it-with-rising-depth (clause b), the SAME
rule holds for the opponent's resulting roster, you keep the edge, and you don't
fall behind. Gains are in real points/week on the RESULTING roster — not a
per-player contextual-value sum.

Fixture numbers double as forward_ppg (so lineup_strength_ppg is meaningful), and
the gate threshold applies in those points/week units.
"""
from __future__ import annotations

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.lineup import LineupPlayer
from backend.services.trade.trade_proposals import (
    Candidate,
    _LINEUP_GAIN_THRESHOLD,
    enumerate_candidates,
    evaluate_candidates,
    evaluate_edge_band,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _lp(spec):
    # forward_ppg == forward_value here so lineup_strength_ppg is exercised.
    return [LineupPlayer(pid, pos, fv, forward_ppg=fv) for pid, pos, fv in spec]


# Me: RB-rich / WR-thin (only 2 WR → an empty WR3 slot an incoming WR fills);
# Them: WR-rich / RB-thin. The asymmetry that makes a positive-sum trade exist.
ME_STRONG = [("qm", "QB", 22), ("rm1", "RB", 24), ("rm2", "RB", 22), ("rm3", "RB", 20),
             ("rm4", "RB", 15), ("rm5", "RB", 13),  # rm4/rm5: surplus RBs (below my FLEX)
             ("wm1", "WR", 16), ("wm2", "WR", 14), ("tm", "TE", 15)]
THEM = [("qt", "QB", 19), ("rt1", "RB", 9), ("btr", "RB", 7),
        ("wt1", "WR", 20), ("wt2", "WR", 18), ("wt3", "WR", 16), ("wt4", "WR", 14),
        ("wt5", "WR", 13), ("wt6", "WR", 12),  # wt5/wt6: surplus WRs (benched)
        ("tt", "TE", 14)]


# ---------------------------------------------------------------------------
# evaluate_edge_band — the lineup-objective gate
# ---------------------------------------------------------------------------
def test_mutual_benefit_trade_clears_on_lineup_improvement():
    # Give my surplus RB (rm4) for their surplus WR (wt5): wt5 fills my empty WR3
    # slot (lineup up), rm4 starts at RB for the RB-thin opponent (their lineup up).
    e = evaluate_edge_band(_lp(ME_STRONG), _lp(THEM), ["rm4"], ["wt5"])
    assert e.clears is True
    assert e.your_lineup_gain >= _LINEUP_GAIN_THRESHOLD   # 1: my starting lineup improves
    assert e.their_lineup_gain >= _LINEUP_GAIN_THRESHOLD  # 2: theirs improves too
    assert e.your_lineup_gain > e.their_lineup_gain       # 3: I keep the edge
    assert e.my_strength >= e.their_strength              # 4: I don't fall behind


def test_robbery_is_rejected_because_their_lineup_craters():
    # I give a scrub, get their RB stud — great for me, but they lose their RB1, so
    # THEIR lineup falls → fails the "acceptable to them" clause. Was a "win" under
    # the old winner=="you" / value-sum bar.
    me = _lp([("q", "QB", 22), ("r1", "RB", 20), ("r2", "RB", 18),
              ("w1", "WR", 16), ("w2", "WR", 14), ("w3", "WR", 12),
              ("t", "TE", 13), ("scrub", "WR", 5)])
    them = _lp([("q2", "QB", 19), ("stud", "RB", 24), ("r9", "RB", 9),
                ("w4", "WR", 15), ("w5", "WR", 13), ("w6", "WR", 11), ("t2", "TE", 10)])
    e = evaluate_edge_band(me, them, ["scrub"], ["stud"])
    assert e.your_lineup_gain > 0          # I'd clearly improve (old bar would surface this)
    assert e.their_lineup_gain < 0         # but their lineup falls → they'd reject
    assert e.clears is False


def test_both_lineups_improve_in_a_positive_sum_trade():
    # A real positive-sum trade improves BOTH starting lineups — only representable
    # because value is roster-relative (the swap fills each side's hole).
    e = evaluate_edge_band(_lp(ME_STRONG), _lp(THEM), ["rm4"], ["wt5"])
    assert e.your_lineup_gain > 0 and e.their_lineup_gain > 0


def test_condition_4_allows_an_already_behind_team_to_trade_up():
    # No-overtake-only guard (#168): my roster is weak, I'm behind pre- and post-
    # trade, but the trade causes no ahead→behind flip → c4 doesn't block. The
    # incoming WR still fills my empty WR3 slot, so my lineup improves.
    me_weak = _lp([("qm", "QB", 8), ("rm1", "RB", 24), ("rm2", "RB", 22),
                   ("rm3", "RB", 20), ("rm4", "RB", 15),
                   ("wm1", "WR", 6), ("wm2", "WR", 5), ("tm", "TE", 6)])
    e = evaluate_edge_band(me_weak, _lp(THEM), ["rm4"], ["wt5"])
    assert e.your_lineup_gain > 0 and e.their_lineup_gain > 0
    assert e.my_strength < e.their_strength    # I stay behind on the field...
    assert e.clears is True                    # ...but c4 (no-overtake) no longer blocks it


# ---------------------------------------------------------------------------
# evaluate_candidates — surfacing, never-pad, ranking (LeagueState path)
# ---------------------------------------------------------------------------
def _iv(pid, pos, fv, *, buy_low=False, trend=ValueTrend.STABLE):
    return InSeasonValue(
        canonical_player_id=pid, name=f"P-{pid}", position=pos, forward_value=fv,
        value_trend=trend, buy_low=buy_low, sell_high=False, why="",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
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
    assert edge.clears and edge.your_lineup_gain >= _LINEUP_GAIN_THRESHOLD


def test_never_pads_when_nothing_clears():
    # I'm strictly stronger at every position → no swap improves my lineup.
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


def test_cleared_candidates_ranked_by_lineup_gain_descending():
    state, values = _league(ME_STRONG, THEM)
    cands = [Candidate(("rm4",), ("wt5",), "opp"),
             Candidate(("rm4",), ("wt6",), "opp"),
             Candidate(("rm5",), ("wt6",), "opp")]
    out = evaluate_candidates(state, values, "me", cands, roster_limit=16)
    assert len(out) >= 2
    gains = [edge.your_lineup_gain for _, _, edge in out]
    assert gains == sorted(gains, reverse=True)    # highest lineup gain first


def test_cap_at_five():
    state, values = _league(ME_STRONG, THEM)
    cands = [Candidate(("rm4",), (w,), "opp") for w in ("wt5", "wt4", "wt3", "wt2", "wt1")]
    out = evaluate_candidates(state, values, "me", cands * 3, roster_limit=16)
    assert len(out) <= 5


def test_enumerate_candidates_is_now_targeted_need_surplus():
    # Slice 6 targeting unchanged: my surplus RBs (rm4/rm5) ↔ their RB need; their
    # surplus WRs (wt5/wt6) ↔ my WR need. give-pool {rm4,rm5} × get-pool {wt5,wt6}.
    state, values = _league(ME_STRONG, THEM)
    cands = enumerate_candidates(state, values, "me")
    assert len(cands) == 9
    assert all(c.counterparty_team_id == "opp" for c in cands)
    assert {p for c in cands for p in c.give_ids} == {"rm4", "rm5"}
    assert {p for c in cands for p in c.get_ids} == {"wt5", "wt6"}
