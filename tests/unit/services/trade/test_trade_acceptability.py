"""
Acceptability READ tests (slice 5, trade_acceptability_design.md §5/§6c). The
analyzer evaluates ANY trade and reports whether the OTHER side would likely
accept it — derived from the slice-4 edge band, viewed from the counterparty's
perspective. The headline: a trade that's GREAT FOR YOU but they'd reject is
reported as a rejection (their_net <= 0), never rounded up to a win. It is a
READ, not a gate — every scenario below is evaluated, none is filtered out.
"""
from __future__ import annotations

import backend.services.trade.trade_proposals as tp
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_proposals import (
    ACCEPT_LIKELY,
    ACCEPT_MARGINAL,
    ACCEPT_REJECT,
    _LINEUP_GAIN_THRESHOLD,
    _MAINTAIN_TOL,
    _lineup_roster,
    acceptability_read,
    evaluate_edge_band,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend

# Me RB-rich / WR-thin (strong); them WR-rich / RB-thin — the slice-2 asymmetry
# that makes positive-sum trades exist. (Same shapes as the edge-band tests.)
ME_STRONG = [("qm", "QB", 22), ("rm1", "RB", 24), ("rm2", "RB", 22), ("rm3", "RB", 20),
             ("rm4", "RB", 15), ("rm5", "RB", 13), ("wm1", "WR", 16), ("wm2", "WR", 14),
             ("tm", "TE", 15)]
ME_WEAK = [("qm", "QB", 8), ("rm1", "RB", 24), ("rm2", "RB", 22), ("rm3", "RB", 20),
           ("rm4", "RB", 15), ("wm1", "WR", 6), ("wm2", "WR", 5), ("tm", "TE", 6)]
THEM = [("qt", "QB", 19), ("rt1", "RB", 9), ("btr", "RB", 7),
        ("wt1", "WR", 20), ("wt2", "WR", 18), ("wt3", "WR", 16), ("wt4", "WR", 14),
        ("wt5", "WR", 13), ("wt6", "WR", 12), ("tt", "TE", 14)]


def _iv(pid, pos, fv):
    # forward_ppg == forward_value so lineup_strength_ppg (the acceptability basis)
    # is exercised in the same units as the gate threshold.
    return InSeasonValue(
        canonical_player_id=pid, name=f"P-{pid}", position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
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


# ---------------------------------------------------------------------------
# LIKELY ACCEPT — the trade improves THEIR starting lineup (>= threshold)
# ---------------------------------------------------------------------------
def test_likely_accept_when_their_lineup_improves():
    state, values = _league(ME_STRONG, THEM)
    # Give my surplus RB (rm4) for their surplus WR (wt5): rm4 starts at RB for the
    # RB-thin opponent → their lineup improves comfortably.
    acc = acceptability_read(state, values, "me", ["rm4"], ["wt5"], hedged=False)
    assert acc.verdict == ACCEPT_LIKELY
    assert acc.their_lineup_gain > _MAINTAIN_TOL   # they clearly improve
    assert acc.overtake_flag is False             # I stay stronger on the field
    assert "improves" in acc.why                   # grounded in their roster


# ---------------------------------------------------------------------------
# LIKELY REJECT — the headline: great for YOU, their lineup falls → they'd reject
# ---------------------------------------------------------------------------
def test_likely_reject_when_trade_is_a_robbery_for_you():
    # I give a scrub WR (5) and get their RB stud (24): huge for me, but they LOSE
    # their RB1 → THEIR lineup falls → likely_reject, NOT a win.
    me = [("q", "QB", 22), ("r1", "RB", 20), ("r2", "RB", 18), ("w1", "WR", 16),
          ("w2", "WR", 14), ("w3", "WR", 12), ("t", "TE", 13), ("scrub", "WR", 5)]
    them = [("q2", "QB", 19), ("stud", "RB", 24), ("r9", "RB", 9), ("w4", "WR", 15),
            ("w5", "WR", 13), ("w6", "WR", 11), ("t2", "TE", 10)]
    state, values = _league(me, them)
    acc = acceptability_read(state, values, "me", ["scrub"], ["stud"], hedged=False)
    assert acc.verdict == ACCEPT_REJECT
    assert acc.their_lineup_gain < -_MAINTAIN_TOL   # their lineup falls below the maintain bar
    assert "drops" in acc.why                       # honest: it guts their lineup


# ---------------------------------------------------------------------------
# CONSISTENCY (headline) — a trade where the opponent MODESTLY improves at fair
# value is now likely_accept (was marginal/reject under the old >=5 bar), and the
# proposal gate + the analyzer read AGREE on it (the Bijan-class fix, #174 align).
# ---------------------------------------------------------------------------
def test_modest_fair_improvement_is_accept_and_agrees_with_gate():
    # Give them a modest surplus RB (10) — their RB-thin lineup improves a little
    # (~1 ppg, below the old 5 bar) AND it's fair value. Maintain + fair → accept.
    me = [("q", "QB", 22), ("r1", "RB", 24), ("r2", "RB", 22), ("r3", "RB", 20),
          ("rmid", "RB", 10), ("w1", "WR", 16), ("w2", "WR", 14), ("t", "TE", 15)]
    state, values = _league(me, THEM)
    acc = acceptability_read(state, values, "me", ["rmid"], ["wt6"], hedged=False)
    assert acc.verdict == ACCEPT_LIKELY                  # maintain+fair → accept (was marginal)
    assert 0 < acc.their_lineup_gain < _LINEUP_GAIN_THRESHOLD   # modest, below the old bar
    # The proposal gate and the analyzer agree: this trade CLEARS the gate too.
    edge = evaluate_edge_band(_lineup_roster(state.teams[0], values),
                              _lineup_roster(state.teams[1], values), ["rmid"], ["wt6"], roster_limit=16)
    assert edge.clears is True


def test_pure_lateral_is_marginal():
    # Give a low RB (6) that doesn't crack their RB-thin lineup (9,7) for a benched
    # WR — their lineup is unchanged (~0, within the maintain band) and it's fair →
    # marginal (a lateral they may not jump at), not a clear accept.
    me = [("q", "QB", 22), ("r1", "RB", 24), ("r2", "RB", 22), ("r3", "RB", 20),
          ("rlow", "RB", 6), ("w1", "WR", 16), ("w2", "WR", 14), ("t", "TE", 15)]
    state, values = _league(me, THEM)
    acc = acceptability_read(state, values, "me", ["rlow"], ["wt6"], hedged=False)
    assert acc.verdict == ACCEPT_MARGINAL
    assert -_MAINTAIN_TOL <= acc.their_lineup_gain <= _MAINTAIN_TOL
    assert "lateral" in acc.why


# ---------------------------------------------------------------------------
# REVERSE-FLEECE — opponent's lineup MAINTAINS but they'd hand over far more
# value than they get → likely_reject (value-fairness, same as the gate's cond 3).
# ---------------------------------------------------------------------------
def test_reverse_fleece_is_labeled_reject():
    # Acquire a startable bench WR (44, benched on a WR-loaded opp) for a junk QB
    # (5). Their lineup maintains (he was benched) but ratio 8.8 → not fair → reject.
    me = [("q", "QB", 20), ("r1", "RB", 22), ("r2", "RB", 20), ("w1", "WR", 16),
          ("w2", "WR", 14), ("w3", "WR", 8), ("t", "TE", 15), ("junkqb", "QB", 5)]
    opp = [("oq", "QB", 19), ("orb1", "RB", 16), ("orb2", "RB", 14),
           ("ow1", "WR", 50), ("ow2", "WR", 48), ("ow3", "WR", 46), ("ow4", "WR", 45),
           ("mclaurin", "WR", 44), ("ote", "TE", 13)]
    state, values = _league(me, opp)
    acc = acceptability_read(state, values, "me", ["junkqb"], ["mclaurin"], hedged=False)
    assert acc.verdict == ACCEPT_REJECT
    assert acc.their_lineup_gain >= -_MAINTAIN_TOL    # their lineup MAINTAINS...
    assert "value" in acc.why                          # ...but it's a value fleece


# ---------------------------------------------------------------------------
# ONE SOURCE OF TRUTH — the analyzer label and the gate read the SAME constants,
# so they can't drift. Moving _FAIRNESS_RATIO flips BOTH together.
# ---------------------------------------------------------------------------
def test_analyzer_and_gate_share_fairness_constant(monkeypatch):
    # A fair-but-lopsided trade (acquirer ratio 3.5): give a surplus RB (10) for a
    # benched WR (35). At R=5 it's fair → gate clears + analyzer accepts; tighten
    # R to 2 and BOTH flip — the gate stops clearing AND the analyzer says reject.
    me = [("q", "QB", 22), ("r1", "RB", 24), ("r2", "RB", 22), ("r3", "RB", 20),
          ("rlow", "RB", 10), ("w1", "WR", 16), ("w2", "WR", 14), ("t", "TE", 15)]
    opp = [("oq", "QB", 19), ("orb1", "RB", 9), ("orb2", "RB", 7),
           ("ow1", "WR", 44), ("ow2", "WR", 42), ("ow3", "WR", 40), ("ow4", "WR", 38),
           ("benchwr", "WR", 35), ("ote", "TE", 14)]
    state, values = _league(me, opp)
    mlp, tlp = _lineup_roster(state.teams[0], values), _lineup_roster(state.teams[1], values)

    def both():
        edge = evaluate_edge_band(mlp, tlp, ["rlow"], ["benchwr"], roster_limit=16)
        acc = acceptability_read(state, values, "me", ["rlow"], ["benchwr"], hedged=False)
        return edge.clears, acc.verdict

    monkeypatch.setattr(tp, "_FAIRNESS_RATIO", 5.0)
    clears5, verdict5 = both()
    assert clears5 is True and verdict5 != ACCEPT_REJECT   # fair @ R=5 → gate clears + analyzer accepts

    monkeypatch.setattr(tp, "_FAIRNESS_RATIO", 2.0)
    clears2, verdict2 = both()
    assert clears2 is False and verdict2 == ACCEPT_REJECT  # ratio 3.5 > 2 → BOTH flip together


# ---------------------------------------------------------------------------
# OVERTAKE FLAG — they'd accept AND it makes their lineup overtake yours
# ---------------------------------------------------------------------------
def test_overtake_flag_when_trade_lets_them_pass_you_on_the_field():
    state, values = _league(ME_WEAK, THEM)
    # Same beneficial swap, but my roster is weak everywhere except the RB I send
    # → I fall behind on the field even though their lineup happily improves.
    acc = acceptability_read(state, values, "me", ["rm4"], ["wt5"], hedged=False)
    assert acc.verdict == ACCEPT_LIKELY            # their lineup still improves
    assert acc.overtake_flag is True               # but it makes them stronger than me
    assert "stronger than yours" in acc.why


# ---------------------------------------------------------------------------
# HEDGE — opponent-side data is thin → the read is tentative
# ---------------------------------------------------------------------------
def test_hedge_softens_the_read():
    state, values = _league(ME_STRONG, THEM)
    acc = acceptability_read(state, values, "me", ["rm4"], ["wt5"], hedged=True)
    assert acc.hedged is True
    assert "tentative" in acc.why                  # the read is explicitly softened


# ---------------------------------------------------------------------------
# Degrades safely when the counterparty can't be resolved (never a gate/raise)
# ---------------------------------------------------------------------------
def test_safe_read_when_counterparty_not_found():
    state, values = _league(ME_STRONG, THEM)
    # "ghost" isn't on any team → no counterparty holds it.
    acc = acceptability_read(state, values, "me", ["rm4"], ["ghost"], hedged=False)
    assert acc.verdict == ACCEPT_REJECT
    assert acc.their_lineup_gain == 0.0
