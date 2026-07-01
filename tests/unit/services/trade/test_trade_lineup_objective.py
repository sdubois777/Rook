"""
Acceptance tests for the LINEUP-IMPROVEMENT objective (trade_lineup_value_design.md
§8). A trade is judged by the change in your STARTING LINEUP's points/week on the
RESULTING roster — not the sum of the players' values. These lock the headline
behaviors: the proof trade reads honestly and doesn't surface, forced drops are
debited, the asymmetric gate (acquirer improves, opponent maintains, value-fair),
and the verdict reads in points/week.
"""
from __future__ import annotations

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_analysis import analyze_trade
from backend.services.trade.trade_proposals import (
    Candidate,
    _FAIRNESS_RATIO,
    _LINEUP_GAIN_THRESHOLD,
    _MAINTAIN_TOL,
    _lineup_roster,
    evaluate_candidates,
    evaluate_edge_band,
    _value_fair,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _iv(pid, pos, fv, *, buy_low=False):
    return InSeasonValue(
        canonical_player_id=pid, name=f"P-{pid}", position=pos, forward_value=fv,
        value_trend=ValueTrend.RISING if buy_low else ValueTrend.STABLE,
        buy_low=buy_low, sell_high=False, why="",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _state(my_spec, opp_spec):
    me = TeamState("me", "Me", True,
                   tuple(RosterPlayer(p, f"P-{p}", pos) for p, pos, *_ in my_spec))
    opp = TeamState("opp", "Opp", False,
                    tuple(RosterPlayer(p, f"P-{p}", pos) for p, pos, *_ in opp_spec))
    state = LeagueState(2025, 14, (me, opp))
    values = {}
    for spec in (my_spec, opp_spec):
        for p, pos, fv, *rest in spec:
            values[p] = _iv(p, pos, fv, buy_low=bool(rest and rest[0]))
    return state, values


# ---------------------------------------------------------------------------
# §8.1 PROOF TRADE — give 1 bench RB, get 3 WRs where only one marginally helps
# and two forced drops are needed. NOT "lopsided you win"; tiny gain; no surface.
# ---------------------------------------------------------------------------
# Me: a strong WR corps (3 starters + a strong FLEX), so an incoming WR only
# marginally beats the flex; the other incoming WRs ride the bench (flat usage).
_PROOF_ME = [("qb", "QB", 20), ("rb1", "RB", 22), ("rb2", "RB", 18),
             ("wr1", "WR", 22), ("wr2", "WR", 21), ("wr3", "WR", 20), ("wr4", "WR", 19),
             ("te", "TE", 15), ("rbench", "RB", 10), ("junk1", "WR", 5), ("junk2", "WR", 4)]
_PROOF_OPP = [("oqb", "QB", 18), ("orb1", "RB", 9), ("orb2", "RB", 8),
              ("btj", "WR", 20), ("odunze", "WR", 18), ("helm", "TE", 7),
              ("ow1", "WR", 14), ("ow2", "WR", 12)]


def test_proof_trade_reads_honestly_and_does_not_surface():
    state, values = _state(_PROOF_ME, _PROOF_OPP)
    give, get = ["rbench"], ["btj", "odunze", "helm"]

    a = analyze_trade(state, values, "me", give, get, roster_limit=11)
    assert a.lineup_gain < _LINEUP_GAIN_THRESHOLD          # NOT a big win
    assert "lopsided you" not in a.fairness                # never "lopsided you win"
    assert a.roster_guard.triggered                        # +2 players → forced drops

    # Through the gate: a flat-usage bench haul that barely moves the lineup does
    # NOT surface (fails clause (a) and, with no rising incoming bench, clause (b)).
    out = evaluate_candidates(state, values, "me", [Candidate(tuple(give), tuple(get), "opp")], roster_limit=11)
    assert out == []


# ---------------------------------------------------------------------------
# §8.3 FORCED DROPS DEBITED — downgrading the lineup to add scrubs reads as a loss
# ---------------------------------------------------------------------------
def test_forced_drop_of_a_starter_reads_as_a_loss():
    # Exactly-full roster of starters; give a WR starter, get two scrub WRs → net
    # +1 forces a drop. The scrubs can't start, so the resulting lineup is WORSE.
    me = [("qb", "QB", 20), ("rb1", "RB", 22), ("rb2", "RB", 20),
          ("wr1", "WR", 22), ("wr2", "WR", 20), ("wr3", "WR", 16), ("te", "TE", 15),
          ("flx", "RB", 14)]   # 8 players, all start (2RB+FLEX from rb1/rb2/flx)
    opp = [("oqb", "QB", 18), ("scrub1", "WR", 3), ("scrub2", "WR", 2),
           ("ow1", "WR", 15), ("ow2", "WR", 13), ("orb", "RB", 9)]
    state, values = _state(me, opp)
    a = analyze_trade(state, values, "me", ["wr3"], ["scrub1", "scrub2"], roster_limit=8)
    assert a.lineup_gain < 0          # gave a 16-ppg starter for scrubs → lineup falls
    assert a.winner == "opponent"


# ---------------------------------------------------------------------------
# §8.4 GENUINE UPGRADE SURFACES — a real 5+ ppg starter upgrade clears clause (a)
# ---------------------------------------------------------------------------
def test_genuine_starter_upgrade_clears_clause_a():
    # Mutual surplus-for-need: I'm RB-rich / WR-thin (empty WR3), they're WR-rich /
    # RB-thin. Give my surplus RB (fills their RB hole, +their lineup), get their
    # surplus WR (fills my WR3, +my lineup). BOTH lineups improve 5+; I gain more.
    me = [("qb", "QB", 22), ("rb1", "RB", 24), ("rb2", "RB", 22), ("rb3", "RB", 20),
          ("rsurplus", "RB", 15), ("wr1", "WR", 16), ("wr2", "WR", 14), ("te", "TE", 15)]
    opp = [("oqb", "QB", 19), ("orb1", "RB", 9), ("orb2", "RB", 7),
           ("ow1", "WR", 20), ("ow2", "WR", 18), ("ow3", "WR", 16), ("ow4", "WR", 14),
           ("wsurplus", "WR", 13), ("ote", "TE", 14)]
    state, values = _state(me, opp)
    out = evaluate_candidates(state, values, "me", [Candidate(("rsurplus",), ("wsurplus",), "opp")], roster_limit=16)
    assert len(out) == 1
    _, a, edge = out[0]
    assert edge.your_lineup_gain >= _LINEUP_GAIN_THRESHOLD
    assert a.lineup_gain >= _LINEUP_GAIN_THRESHOLD and a.winner == "you"


# ---------------------------------------------------------------------------
# ASYMMETRIC GATE — the value-fairness condition (cond 3, anti-reverse-fleece)
# ---------------------------------------------------------------------------
def test_value_fairness_ratio_brackets_the_calibration_cases():
    # The three measured calibration ratios: McLaurin-fleece 16.7 (FAIL), Swift
    # 3.92 (PASS), Bijan 1.30 (PASS). R=5 brackets them cleanly.
    assert _value_fair(get_val=96.9, give_val=5.8) is False   # McLaurin: ratio 16.7 > R
    assert _value_fair(get_val=89.4, give_val=22.8) is True    # Swift: ratio 3.92 < R
    assert _value_fair(get_val=94.6, give_val=72.9) is True    # Bijan: ratio 1.30
    # boundary: just under R passes, just over fails (ratio, not gap)
    assert _value_fair(get_val=4.9, give_val=1.0) is True      # 4.9 < 5
    assert _value_fair(get_val=5.1, give_val=1.0) is False     # 5.1 > 5
    assert _value_fair(get_val=1.0, give_val=5.1) is False     # reverse direction guarded
    assert _value_fair(get_val=10.0, give_val=0.0) is False    # give nothing of value


def test_reverse_fleece_fails_cond3_even_though_cond1_and_2_pass():
    # ACQUIRE a startable bench WR (44) + scraps for a junk QB (5): my lineup
    # improves (cond 1), the opponent only benched the WR so they maintain (cond 2),
    # but I'm getting ~16x the value I give → cond 3 (value-fairness) kills it.
    me = [("qb", "QB", 20), ("rb1", "RB", 22), ("rb2", "RB", 20), ("rb3", "RB", 18),
          ("wr1", "WR", 16), ("wr2", "WR", 14), ("wr3", "WR", 8), ("te", "TE", 15),
          ("junkqb", "QB", 5)]
    # opp is WR-LOADED: McLaurin(44) is their benched WR5 (all 4 starters score >44)
    # → giving him MAINTAINS their lineup, so cond 2 passes and only cond 3 can kill it.
    opp = [("oqb", "QB", 19), ("orb1", "RB", 16), ("orb2", "RB", 14),
           ("ow1", "WR", 50), ("ow2", "WR", 48), ("ow3", "WR", 46), ("ow4", "WR", 45),
           ("mclaurin", "WR", 44), ("ote", "TE", 13)]
    state, values = _state(me, opp)
    e = evaluate_edge_band(_lineup_roster(state.my_team, values), _lineup_roster(state.teams[1], values),
                           ["junkqb"], ["mclaurin"], roster_limit=16)
    assert e.your_lineup_gain >= _LINEUP_GAIN_THRESHOLD     # cond 1 passes (McLaurin upgrades my WR3)
    assert e.their_lineup_gain >= -_MAINTAIN_TOL            # cond 2 passes (they benched him → maintain)
    assert e.clears is False                                # but cond 3 (value-fair) kills the fleece




# ---------------------------------------------------------------------------
# §8.7 VERDICT IN POINTS/WEEK — the headline is Δlineup ppg, not a value-delta
# ---------------------------------------------------------------------------
def test_verdict_headline_is_lineup_points_per_week():
    me = [("qb", "QB", 20), ("rb1", "RB", 22), ("rb2", "RB", 20), ("rb3", "RB", 18),
          ("wr1", "WR", 18), ("wr2", "WR", 16), ("wr3", "WR", 10), ("te", "TE", 15),
          ("rbench", "RB", 12)]
    opp = [("oqb", "QB", 18), ("orb1", "RB", 8), ("orb2", "RB", 7), ("stud", "WR", 22),
           ("ow1", "WR", 13), ("ow2", "WR", 11), ("ote", "TE", 9)]
    state, values = _state(me, opp)
    a = analyze_trade(state, values, "me", ["rbench"], ["stud"], roster_limit=16)
    # lineup_gain is the real starting-lineup ppg change (stud 22 replaces wr3 10 = +12),
    # NOT the raw value_delta (which here is also ~10 but is grounding, not the verdict).
    assert a.lineup_gain == 12.0
    assert a.winner == "you"
