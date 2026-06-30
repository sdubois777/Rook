"""
Acceptability READ tests (slice 5, trade_acceptability_design.md §5/§6c). The
analyzer evaluates ANY trade and reports whether the OTHER side would likely
accept it — derived from the slice-4 edge band, viewed from the counterparty's
perspective. The headline: a trade that's GREAT FOR YOU but they'd reject is
reported as a rejection (their_net <= 0), never rounded up to a win. It is a
READ, not a gate — every scenario below is evaluated, none is filtered out.
"""
from __future__ import annotations

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_proposals import (
    ACCEPT_LIKELY,
    ACCEPT_MARGINAL,
    ACCEPT_REJECT,
    _LINEUP_GAIN_THRESHOLD,
    acceptability_read,
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
    assert acc.their_lineup_gain >= _LINEUP_GAIN_THRESHOLD
    assert acc.overtake_flag is False             # I stay stronger on the field
    assert "need" in acc.why                      # grounded in their roster


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
    assert acc.their_lineup_gain <= 0              # their lineup falls
    assert "little" in acc.why                     # honest: no value to them


# ---------------------------------------------------------------------------
# MARGINAL — small lineup improvement, below the threshold → they may haggle
# ---------------------------------------------------------------------------
def test_marginal_when_their_lineup_barely_improves():
    # Give them a modest surplus RB (10) — their RB-thin lineup (9, 7) improves only
    # a little (a ~3 ppg bump), below the threshold → marginal.
    me = [("q", "QB", 22), ("r1", "RB", 24), ("r2", "RB", 22), ("r3", "RB", 20),
          ("rmid", "RB", 10), ("w1", "WR", 16), ("w2", "WR", 14), ("t", "TE", 15)]
    state, values = _league(me, THEM)
    acc = acceptability_read(state, values, "me", ["rmid"], ["wt6"], hedged=False)
    assert acc.verdict == ACCEPT_MARGINAL
    assert 0 < acc.their_lineup_gain < _LINEUP_GAIN_THRESHOLD
    assert "haggle" in acc.why


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
