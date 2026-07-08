"""
Matchup scouting primitives — schedule synthesis (deterministic, even/odd),
the positional grid (sums to lineup_strength_ppg), and the APPROXIMATE win-prob
band (margin-derived, confidence-widened). All pure — no DB, no Sonnet.
"""
from __future__ import annotations

import logging

from backend.services.matchup.scouting import (
    GRID_POSITIONS,
    SURPLUS_MARGIN_PPW,
    confidence_summary,
    leverage_readout,
    opponent_of,
    positional_slot_ppg,
    synthesize_week_matchups,
    value_gated_surplus_positions,
    win_prob_band,
)
from backend.services.trade.lineup import DEFAULT_LINEUP_RULES, LineupPlayer, lineup_strength_ppg
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


# ---------------------------------------------------------------------------
# schedule synthesis
# ---------------------------------------------------------------------------
def _ids(n):
    return [f"t{i}" for i in range(n)]


def test_schedule_pairs_every_team_exactly_once():
    ms = synthesize_week_matchups(_ids(12), week=14)
    assert len(ms) == 6
    seen = [t for m in ms for t in (m.home_team_id, m.away_team_id)]
    assert sorted(seen) == sorted(_ids(12))       # all 12, no repeats
    assert all(not m.is_complete and m.home_score == 0.0 for m in ms)  # forward preview


def test_schedule_is_deterministic():
    a = synthesize_week_matchups(_ids(12), week=14)
    b = synthesize_week_matchups(_ids(12), week=14)
    assert [(m.home_team_id, m.away_team_id) for m in a] == [(m.home_team_id, m.away_team_id) for m in b]


def test_schedule_rotates_by_week():
    # Different weeks give different pairings (a real schedule, not a fixed pairing).
    w14 = {frozenset((m.home_team_id, m.away_team_id)) for m in synthesize_week_matchups(_ids(12), 14)}
    w15 = {frozenset((m.home_team_id, m.away_team_id)) for m in synthesize_week_matchups(_ids(12), 15)}
    assert w14 != w15


def test_odd_count_loud_warns_and_byes_one(caplog):
    with caplog.at_level(logging.WARNING):
        ms = synthesize_week_matchups(_ids(11), week=14)
    assert len(ms) == 5                            # 10 paired, 1 bye
    assert "ODD team count" in caplog.text and "BYE" in caplog.text
    paired = {t for m in ms for t in (m.home_team_id, m.away_team_id)}
    assert len(paired) == 10                       # exactly one team byed


def test_opponent_of():
    ms = synthesize_week_matchups(_ids(4), week=1)
    for m in ms:
        assert opponent_of(ms, m.home_team_id) == m.away_team_id
        assert opponent_of(ms, m.away_team_id) == m.home_team_id
    assert opponent_of(ms, "nope") is None


# ---------------------------------------------------------------------------
# positional grid — the sum-to-ppw invariant
# ---------------------------------------------------------------------------
def _lp(pid, pos, fv, ppg):
    return LineupPlayer(pid, pos, fv, forward_ppg=ppg)


def _roster():
    # A near-full demo-shape roster (QB, 2RB, 3WR, TE, K, DEF) + a FLEX-worthy WR.
    return [
        _lp("qb", "QB", 80, 20.0),
        _lp("rb1", "RB", 70, 15.0), _lp("rb2", "RB", 60, 12.0),
        _lp("wr1", "WR", 75, 16.0), _lp("wr2", "WR", 65, 13.0), _lp("wr3", "WR", 55, 11.0),
        _lp("wr4", "WR", 50, 10.0),  # FLEX filler
        _lp("te", "TE", 60, 12.0),
        _lp("k", "K", 40, 8.0),
        _lp("def", "DEF", 45, 9.0),
    ]


def test_grid_sums_to_lineup_strength_ppg():
    roster = _roster()
    repl = {"QB": 12.0, "RB": 8.0, "WR": 9.0, "TE": 6.0, "K": 7.0, "DEF": 7.0}
    grid = positional_slot_ppg(roster, DEFAULT_LINEUP_RULES, repl)
    total = round(sum(grid.values()), 2)
    assert total == lineup_strength_ppg(roster, DEFAULT_LINEUP_RULES, repl)  # THE invariant
    assert set(grid) == set(GRID_POSITIONS)


def test_grid_flex_attributes_to_filling_position():
    # wr4 fills FLEX → its ppg lands on WR, not a FLEX bucket.
    grid = positional_slot_ppg(_roster(), DEFAULT_LINEUP_RULES, None)
    # 3 WR starters (16+13+11) + wr4 FLEX (10) = 50 on WR.
    assert grid["WR"] == 50.0


def test_grid_empty_required_slot_credits_replacement():
    roster = [_lp("qb", "QB", 80, 20.0)]           # only a QB; RB/WR/TE/K/DEF slots empty
    repl = {"QB": 12.0, "RB": 8.0, "WR": 9.0, "TE": 6.0, "K": 7.0, "DEF": 7.0}
    grid = positional_slot_ppg(roster, DEFAULT_LINEUP_RULES, repl)
    assert grid["QB"] == 20.0
    assert grid["RB"] == 16.0                       # 2 empty RB slots × 8.0
    assert round(sum(grid.values()), 2) == lineup_strength_ppg(roster, DEFAULT_LINEUP_RULES, repl)


# ---------------------------------------------------------------------------
# win-prob band — approximate, margin-derived, no fabricated %
# ---------------------------------------------------------------------------
def test_band_tiers_from_margin():
    assert win_prob_band(1.5) == "Toss-up"
    assert win_prob_band(5.0) == "Slight edge"
    assert win_prob_band(-5.0) == "Slight underdog"
    assert win_prob_band(15.0) == "Favored"
    assert win_prob_band(-15.0) == "Underdog"
    assert win_prob_band(25.0) == "Heavy favorite"
    assert win_prob_band(-25.0) == "Heavy underdog"


def test_low_confidence_widens_slight_to_tossup():
    # A slight edge on thin data honestly reads as a toss-up (don't over-claim).
    assert win_prob_band(5.0, low_confidence=True) == "Toss-up"
    assert win_prob_band(15.0, low_confidence=True) == "Favored"   # a clear edge survives


# ---------------------------------------------------------------------------
# value-gated surplus + need/surplus reconciliation + reciprocal mirror
# ---------------------------------------------------------------------------
def _iv(pid, pos, ppg):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position=pos, forward_value=ppg * 5,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="", games_played=10,
        usage_recent=0.0, usage_prior=0.0, usage_delta=0.0, recency_ppg=ppg, expected_ppg=ppg,
        opportunity_gap=0.0, sustainable=True, forward_ppg=ppg, schedule_modifier=0.0,
        prior_projection=None, prior_weight=0.0, name_bias_guard_applied=False,
        confidence=Confidence.FULL, confidence_reason="",
    )


_REPL = {"QB": 13.0, "RB": 7.7, "WR": 6.8, "TE": 6.6, "K": 6.0, "DEF": 5.0}


def test_value_gated_surplus_drops_below_replacement_bodies():
    # The McConkeys case: bench RBs below the 7.7 replacement are NOT surplus.
    values = {
        "conner": _iv("conner", "RB", 0.0),      # below repl → not spare
        "harris": _iv("harris", "RB", 0.0),      # below repl → not spare
        "mostert": _iv("mostert", "RB", 2.2),    # below repl → not spare
        "allgeier": _iv("allgeier", "RB", 8.8),  # above repl + margin → real depth
        "spareqb": _iv("spareqb", "QB", 16.0),   # above repl → real depth
    }
    surplus_ids = tuple(values)
    # No needs → RB and QB depth both count.
    out = value_gated_surplus_positions(surplus_ids, needs=frozenset(), values=values, replacement=_REPL)
    assert out == ["QB", "RB"]                    # ordered; dead RBs excluded, Allgeier keeps RB


def test_surplus_excludes_need_positions_no_position_in_both():
    # RB is a need → its above-replacement bench body is NOT "spare" (need wins).
    values = {"allgeier": _iv("allgeier", "RB", 8.8), "spareqb": _iv("spareqb", "QB", 16.0)}
    out = value_gated_surplus_positions(("allgeier", "spareqb"), needs=frozenset({"RB"}),
                                        values=values, replacement=_REPL)
    assert "RB" not in out and out == ["QB"]      # reconciliation: RB can't be both


def test_surplus_margin_excludes_barely_above_replacement():
    # A body only fractionally above replacement isn't tradeable depth.
    values = {"barely": _iv("barely", "WR", _REPL["WR"] + SURPLUS_MARGIN_PPW - 0.1)}
    assert value_gated_surplus_positions(("barely",), frozenset(), values, _REPL) == []


def test_leverage_mirror_requires_reciprocal_value_fit():
    # A: QB depth, needs WR.  B: WR depth, needs QB.  → reciprocal fit.
    values = {
        "a_qb": _iv("a_qb", "QB", 18.0), "b_wr": _iv("b_wr", "WR", 12.0),
    }
    lev = leverage_readout(
        my_needs=frozenset({"WR"}), my_surplus_ids=("a_qb",),
        opp_needs=frozenset({"QB"}), opp_surplus_ids=("b_wr",),
        values=values, replacement=_REPL,
    )
    assert lev.my_surplus_positions == ("QB",) and lev.opp_surplus_positions == ("WR",)
    assert lev.my_surplus_their_needs == ("QB",) and lev.their_surplus_my_needs == ("WR",)
    assert lev.is_reciprocal_fit is True


def test_leverage_one_directional_is_not_a_mirror():
    # B can help A's QB need, but A has nothing B needs → NOT reciprocal.
    values = {"b_qb": _iv("b_qb", "QB", 18.0)}
    lev = leverage_readout(
        my_needs=frozenset({"QB"}), my_surplus_ids=(),          # A has no depth
        opp_needs=frozenset({"RB"}), opp_surplus_ids=("b_qb",),  # B has QB depth
        values=values, replacement=_REPL,
    )
    assert lev.their_surplus_my_needs == ("QB",)   # one direction fits
    assert lev.my_surplus_their_needs == ()        # the other doesn't
    assert lev.is_reciprocal_fit is False          # → honest "no clear fit"


def test_confidence_summary_share_based_not_min():
    full = [Confidence.FULL] * 10
    assert confidence_summary(full) == ("full", False)
    # one thin starter out of 10 must NOT flip the band (the min-rule bug).
    one_thin = [Confidence.FULL] * 9 + [Confidence.INSUFFICIENT]
    note, low = confidence_summary(one_thin)
    assert note == "mostly_full" and low is False
    # a third thin → genuinely low confidence.
    many_thin = [Confidence.FULL] * 6 + [Confidence.LIMITED] * 4
    note2, low2 = confidence_summary(many_thin)
    assert note2 == "thin" and low2 is True
    assert confidence_summary([]) == ("thin", True)
