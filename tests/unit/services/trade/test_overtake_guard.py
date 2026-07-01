"""
Tests for slice 3: roster_strength + apply_trade + the overtake guard
(trade_acceptability_design.md §4). The headline is GUARD FAILS — a trade that
looks good on raw player value but would make the opponent's lineup overtake mine.
"""
from __future__ import annotations

import pytest

from backend.services.trade.lineup import LineupPlayer, optimal_lineup, roster_strength
from backend.services.trade.overtake import (
    PostTrade,
    TradeError,
    apply_trade,
    overtake_guard,
)


def _p(pid, pos, fv):
    return LineupPlayer(player_id=pid, position=pos, forward_value=fv)


# ---------------------------------------------------------------------------
# roster_strength == optimal_lineup strength
# ---------------------------------------------------------------------------
def test_roster_strength_is_optimal_lineup_strength():
    roster = [_p("q", "QB", 20), _p("r1", "RB", 18), _p("r2", "RB", 14),
              _p("w1", "WR", 16), _p("w2", "WR", 15), _p("w3", "WR", 13), _p("t", "TE", 10)]
    assert roster_strength(roster) == optimal_lineup(roster).strength


# ---------------------------------------------------------------------------
# apply_trade
# ---------------------------------------------------------------------------
def test_apply_trade_moves_players_both_ways_without_mutating_inputs():
    mine = [_p("a", "RB", 20), _p("b", "WR", 15)]
    theirs = [_p("x", "RB", 18), _p("y", "WR", 12)]
    post = apply_trade(mine, theirs, give_ids=["a"], get_ids=["x"])

    assert isinstance(post, PostTrade)
    assert {p.player_id for p in post.my_roster} == {"b", "x"}      # got x, gave a
    assert {p.player_id for p in post.their_roster} == {"y", "a"}   # got a, gave x
    # inputs untouched
    assert {p.player_id for p in mine} == {"a", "b"}
    assert {p.player_id for p in theirs} == {"x", "y"}


def test_apply_trade_rejects_player_not_on_giving_roster():
    mine = [_p("a", "RB", 20)]
    theirs = [_p("x", "RB", 18)]
    with pytest.raises(TradeError):
        apply_trade(mine, theirs, give_ids=["x"], get_ids=["a"])   # x isn't mine
    with pytest.raises(TradeError):
        apply_trade(mine, theirs, give_ids=["a"], get_ids=["zzz"]) # zzz isn't theirs


# ---------------------------------------------------------------------------
# Guard passes
# ---------------------------------------------------------------------------
def test_guard_passes_when_i_stay_stronger():
    mine = [_p("q", "QB", 22), _p("r1", "RB", 20), _p("r2", "RB", 18),
            _p("w1", "WR", 17), _p("w2", "WR", 16), _p("w3", "WR", 15),
            _p("t", "TE", 13), _p("benchwr", "WR", 5)]
    theirs = [_p("q2", "QB", 12), _p("r3", "RB", 8), _p("r4", "RB", 7),
              _p("w4", "WR", 9), _p("w5", "WR", 8), _p("w6", "WR", 6), _p("t2", "TE", 5)]
    # Give a scrub, get their best — I gain and stay clearly ahead.
    res = overtake_guard(mine, theirs, give_ids=["benchwr"], get_ids=["w4"])
    assert res.passes is True
    assert res.my_strength >= res.their_strength


# ---------------------------------------------------------------------------
# Guard FAILS — the whole point of §4
# ---------------------------------------------------------------------------
def test_guard_fails_when_trade_lets_them_overtake_despite_looking_good():
    # I'm WR-deep with exactly 2 RBs; they're RB-thin but WR-DEEP (5 WRs) and
    # close behind. I give an RB starter for their WR1 — raw value +7 to me, so
    # it "looks good" — but it guts my RB while they absorb the WR loss and their
    # lineup overtakes mine.
    mine = [_p("q_m", "QB", 18), _p("rb_m1", "RB", 16), _p("rb_m2", "RB", 15),
            _p("wr_m1", "WR", 20), _p("wr_m2", "WR", 19), _p("wr_m3", "WR", 18),
            _p("wr_m4", "WR", 17), _p("te_m", "TE", 12)]
    theirs = [_p("q_t", "QB", 19), _p("rb_t1", "RB", 8), _p("rb_t2", "RB", 7),
              _p("wr_t1", "WR", 22), _p("wr_t2", "WR", 21), _p("wr_t3", "WR", 20),
              _p("wr_t4", "WR", 19), _p("wr_t5", "WR", 18), _p("te_t", "TE", 14)]

    # Pre-trade I'm ahead, 135 vs 130.
    assert roster_strength(mine) == 135.0
    assert roster_strength(theirs) == 130.0

    # Give my RB (15) for their WR1 (22): +7 raw value to me — looks like a win.
    res = overtake_guard(mine, theirs, give_ids=["rb_m2"], get_ids=["wr_t1"])

    assert res.my_strength == 125.0      # my lineup drops (RB hole, redundant WR)
    assert res.their_strength == 134.0   # their deep WR absorbs the loss; RB need filled
    assert res.passes is False           # they overtake → guard correctly blocks it


# ---------------------------------------------------------------------------
# Edge: exactly equal strengths pass (>=, not >)
# ---------------------------------------------------------------------------
def test_guard_passes_on_exactly_equal_post_trade_strengths():
    # 1-QB-each rosters with a straight QB-for-QB swap of equal value → equal.
    mine = [_p("qa", "QB", 20)]
    theirs = [_p("qb", "QB", 20)]
    res = overtake_guard(mine, theirs, give_ids=["qa"], get_ids=["qb"])
    assert res.my_strength == res.their_strength == 20.0
    assert res.passes is True            # equal → passes (the bar is ≥)


# ===========================================================================
# NO-OVERTAKE-ONLY semantics (relative, trade-caused). The guard fails ONLY when
# a trade FLIPS me from ahead-or-tied to behind — not whenever I end up behind.
# ===========================================================================
def test_headline_regression_already_behind_team_trades_up_now_passes():
    # The recon's Gronk→Burrow class: I'm WEAKER than my trade partner pre-trade,
    # the trade IMPROVES me, and I'm still behind after. The OLD absolute guard
    # wrongly blocked this (I "end up behind"); the new relative guard PASSES it —
    # I had no lead to surrender, so no overtake occurred.
    mine = [_p("q", "QB", 10), _p("r1", "RB", 12), _p("r2", "RB", 10), _p("w", "WR", 8)]
    theirs = [_p("q2", "QB", 20), _p("r3", "RB", 22), _p("r4", "RB", 20), _p("w2", "WR", 18)]
    res = overtake_guard(mine, theirs, give_ids=["w"], get_ids=["w2"])
    assert (res.my_strength_pre, res.their_strength_pre) == (40.0, 80.0)   # behind pre
    assert (res.my_strength, res.their_strength) == (50.0, 70.0)           # behind post
    assert res.my_strength > res.my_strength_pre     # the trade improved me
    assert res.my_strength < res.their_strength      # still behind...
    assert res.passes is True                        # ...but PASSES — no flip


def test_leader_narrowing_their_own_lead_but_staying_ahead_passes():
    # I'm ahead; the trade shrinks my lead but I stay ahead → no overtake → passes.
    # (Conditions 1-3 guard the value fairness of the trade itself, not c4.)
    mine = [_p("q", "QB", 22), _p("r1", "RB", 20), _p("r2", "RB", 18), _p("w", "WR", 16)]
    theirs = [_p("q2", "QB", 14), _p("r3", "RB", 12), _p("r4", "RB", 10), _p("w2", "WR", 8)]
    res = overtake_guard(mine, theirs, give_ids=["r1"], get_ids=["r3"])
    assert res.my_strength_pre - res.their_strength_pre == 32.0   # lead 76-44
    assert res.my_strength - res.their_strength == 16.0           # lead 68-52 (narrowed)
    assert res.my_strength >= res.their_strength                 # still ahead
    assert res.passes is True


def test_surrendering_a_tie_into_a_deficit_is_an_overtake_and_fails():
    # Tied pre, behind post — I gave up a tie, which IS an overtake → FAILS.
    mine = [_p("r1", "RB", 12), _p("r2", "RB", 8)]
    theirs = [_p("r3", "RB", 10), _p("r4", "RB", 10)]
    res = overtake_guard(mine, theirs, give_ids=["r1"], get_ids=["r3"])
    assert res.my_strength_pre == res.their_strength_pre == 20.0  # tied pre
    assert res.my_strength == 18.0 and res.their_strength == 22.0  # behind post
    assert res.passes is False


def test_behind_climbing_to_a_tie_passes():
    # Behind pre, tied post — I didn't fall behind (I closed the gap) → passes.
    mine = [_p("r1", "RB", 8), _p("r2", "RB", 8)]
    theirs = [_p("r3", "RB", 10), _p("r4", "RB", 10)]
    res = overtake_guard(mine, theirs, give_ids=["r2"], get_ids=["r3"])
    assert res.my_strength_pre == 16.0 and res.their_strength_pre == 20.0  # behind pre
    assert res.my_strength == res.their_strength == 18.0                   # tied post
    assert res.passes is True
