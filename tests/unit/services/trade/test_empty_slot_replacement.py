"""
Empty-slot REPLACEMENT-FLOOR tests (trade_lineup_value_design follow-up).

An UNFILLABLE required starter slot was scored 0 — unrealistic, since a team with
no TE streams a waiver TE worth ~replacement ppg, not 0. So empty=0 was
SUPPRESSING beneficial trades (a WR-desperate team punting a mediocre TE for two
WRs read lower than reality). Fix: value an unfilled required slot at the
position's REPLACEMENT ppg — the same ``derive_anchors`` replacement the #172
anchors use, read in forward_ppg units (``replacement_ppg_by_position``), applied
symmetrically to both sides. Net: position-punts price HONESTLY (read HIGHER, more
surface), but punting an ELITE starter is still clearly negative, and trades that
keep every slot filled are byte-identical.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    lineup_strength_ppg,
)
from backend.services.trade.value_engine import (
    derive_anchors,
    replacement_ppg_by_position,
    Confidence,
    InSeasonValue,
    ValueTrend,
)


def _lp(pid, pos, ppg):
    # forward_value mirrors ppg here so the optimizer's ranking matches the ppg sum.
    return LineupPlayer(pid, pos, ppg, forward_ppg=ppg)


def _iv(pid, pos, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


# A roster with NO TE (TE slot unfillable), otherwise full: 1QB/2RB/3WR/1TE/1FLEX.
_NO_TE = [_lp("qb", "QB", 20), _lp("rb1", "RB", 18), _lp("rb2", "RB", 16),
          _lp("rb3", "RB", 12), _lp("wr1", "WR", 17), _lp("wr2", "WR", 15),
          _lp("wr3", "WR", 13), _lp("wr4", "WR", 10)]


# ---------------------------------------------------------------------------
# the helper reuses derive_anchors (no new constant)
# ---------------------------------------------------------------------------
def test_replacement_ppg_by_position_reuses_derive_anchors():
    # a pool big enough to derive at least one position, sparse elsewhere (fallback)
    values = {}
    for i in range(30):
        values[f"rb{i}"] = _iv(f"rb{i}", "RB", 20 - i * 0.5)
    for i in range(3):
        values[f"te{i}"] = _iv(f"te{i}", "TE", 12 - i)
    rep = replacement_ppg_by_position(values)
    # identical to calling derive_anchors on the same forward_ppg pool
    ppg_by_pos = {}
    for v in values.values():
        ppg_by_pos.setdefault(v.position, []).append(v.forward_ppg)
    anchors = derive_anchors(ppg_by_pos)
    for pos in ("QB", "RB", "WR", "TE"):
        assert rep[pos] == anchors[pos][0]
    assert rep["RB"] > 0 and rep["TE"] > 0   # ppg, not 0, not forward_value


# ---------------------------------------------------------------------------
# lineup_strength_ppg: unfilled slot = replacement ppg (not 0)
# ---------------------------------------------------------------------------
def test_unfilled_required_slot_valued_at_replacement_not_zero():
    rep = {"QB": 14.0, "RB": 8.0, "WR": 8.0, "TE": 6.0}
    without = lineup_strength_ppg(_NO_TE, DEFAULT_LINEUP_RULES)                 # empty TE = 0
    withfloor = lineup_strength_ppg(_NO_TE, DEFAULT_LINEUP_RULES, rep)          # empty TE = 6.0
    assert withfloor == round(without + rep["TE"], 2)   # exactly the TE replacement added
    assert withfloor > without


def test_no_empty_slot_is_byte_identical_with_or_without_floor():
    full = _NO_TE + [_lp("te1", "TE", 11)]     # now the TE slot fills → no empty slot
    rep = {"QB": 14.0, "RB": 8.0, "WR": 8.0, "TE": 6.0}
    base = lineup_strength_ppg(full, DEFAULT_LINEUP_RULES)
    assert lineup_strength_ppg(full, DEFAULT_LINEUP_RULES, rep) == base   # floor untouched


def test_unfilled_flex_uses_max_eligible_replacement():
    # QB + 1 RB only: 2RB slot short one, 3WR empty, TE empty, FLEX empty. FLEX is
    # RB/WR/TE-eligible → credited the MAX of those replacements.
    roster = [_lp("qb", "QB", 20), _lp("rb1", "RB", 18)]
    rep = {"QB": 14.0, "RB": 9.0, "WR": 7.0, "TE": 5.0}
    got = lineup_strength_ppg(roster, DEFAULT_LINEUP_RULES, rep)
    # filled: QB20 + RB1 18. empty: RB2=9, WR1/2/3=7 each, TE=5, FLEX=max(9,7,5)=9.
    expected = 20 + 18 + 9 + 7 * 3 + 5 + 9
    assert got == round(expected, 2)


# ---------------------------------------------------------------------------
# the punt reprices — HONEST, and it reads HIGHER (empty=0 understated it)
# ---------------------------------------------------------------------------
def test_position_punt_reads_higher_with_the_floor():
    # WR-desperate team (weak WR3) ships its only TE for a strong WR. PRE is FULL
    # (WR3 filled by the weak WR — like The Lord's floored London), so only the
    # POST roster empties a slot (TE). Empty TE was 0; now it's replacement, so the
    # post-roster is worth more → the gain is HIGHER (honest: you'd stream a TE).
    pre = [_lp("qb", "QB", 20), _lp("rb1", "RB", 18), _lp("rb2", "RB", 16),
           _lp("rb3", "RB", 12), _lp("wr1", "WR", 17), _lp("wr2", "WR", 15),
           _lp("wr3", "WR", 4), _lp("te", "TE", 9)]           # FULL lineup, weak WR3
    post = [p for p in pre if p.player_id != "te"] + [_lp("newwr", "WR", 22)]
    rep = {"QB": 14.0, "RB": 8.0, "WR": 8.0, "TE": 6.0}
    gain_zero = round(lineup_strength_ppg(post) - lineup_strength_ppg(pre), 2)
    gain_floor = round(
        lineup_strength_ppg(post, DEFAULT_LINEUP_RULES, rep)
        - lineup_strength_ppg(pre, DEFAULT_LINEUP_RULES, rep), 2)
    assert gain_floor > gain_zero          # empty=0 understated the punt
    # PRE is full (no empty slot), only POST empties TE → floor adds exactly the TE
    # replacement to the post side.
    assert gain_floor == round(gain_zero + rep["TE"], 2)


def test_elite_position_punt_is_still_a_clear_loss():
    # Ship an ELITE TE (ppg 15) on a team that's NOT WR-desperate → the empty slot
    # only recovers replacement (6), so you still lose ~9 net at TE for little WR
    # gain. Stays clearly negative even with the floor.
    pre = [_lp("qb", "QB", 20), _lp("rb1", "RB", 18), _lp("rb2", "RB", 16),
           _lp("elite_te", "TE", 15), _lp("wr1", "WR", 17), _lp("wr2", "WR", 15),
           _lp("wr3", "WR", 14)]
    post = [p for p in pre if p.player_id != "elite_te"] + [_lp("bench_wr", "WR", 9)]
    rep = {"QB": 14.0, "RB": 8.0, "WR": 8.0, "TE": 6.0}
    gain = round(
        lineup_strength_ppg(post, DEFAULT_LINEUP_RULES, rep)
        - lineup_strength_ppg(pre, DEFAULT_LINEUP_RULES, rep), 2)
    assert gain < 0        # elite TE lost (15) recovers only replacement (6) → net loss


def test_empty_qb_slot_uses_qb_replacement():
    # Shipping the only QB leaves the QB slot at QB replacement (streamable), not 0.
    # FULL roster so the ONLY empty slot after shipping the QB is the QB slot.
    pre = [_lp("qb", "QB", 18), _lp("rb1", "RB", 16), _lp("rb2", "RB", 14),
           _lp("rb3", "RB", 10), _lp("wr1", "WR", 15), _lp("wr2", "WR", 13),
           _lp("wr3", "WR", 11), _lp("te", "TE", 10)]        # FULL: QB fills the QB slot
    post = [p for p in pre if p.player_id != "qb"]           # 7 players → only QB slot empty
    rep = {"QB": 13.0, "RB": 8.0, "WR": 8.0, "TE": 6.0}
    with_floor = lineup_strength_ppg(post, DEFAULT_LINEUP_RULES, rep)
    without = lineup_strength_ppg(post, DEFAULT_LINEUP_RULES)
    assert with_floor == round(without + rep["QB"], 2)   # QB replacement, not 0


# ---------------------------------------------------------------------------
# SYMMETRY — both sides' empty slots use the same floor (via evaluate_edge_band)
# ---------------------------------------------------------------------------
def test_edge_band_applies_floor_symmetrically():
    from backend.services.trade.trade_proposals import evaluate_edge_band
    # I ship my only TE; the opponent ships their only QB — both sides end with an
    # empty required slot. Passing replacement_ppg must credit BOTH.
    # FULL rosters (8 players each, no empty slot pre-trade), so only the POST sides
    # empty a slot: I empty TE, they empty QB.
    mine = [_lp("mqb", "QB", 20), _lp("mrb1", "RB", 18), _lp("mrb2", "RB", 16),
            _lp("mrb3", "RB", 12), _lp("mwr1", "WR", 15), _lp("mwr2", "WR", 13),
            _lp("mwr3", "WR", 11), _lp("mte", "TE", 9)]
    theirs = [_lp("tqb", "QB", 19), _lp("trb1", "RB", 17), _lp("trb2", "RB", 15),
              _lp("trb3", "RB", 11), _lp("twr1", "WR", 16), _lp("twr2", "WR", 14),
              _lp("twr3", "WR", 10), _lp("tte", "TE", 12)]
    rep = {"QB": 14.0, "RB": 8.0, "WR": 8.0, "TE": 6.0}
    # I give mte (TE) get tqb (QB) → I empty TE, they empty QB.
    e0 = evaluate_edge_band(mine, theirs, ["mte"], ["tqb"], roster_limit=16)
    ef = evaluate_edge_band(mine, theirs, ["mte"], ["tqb"], roster_limit=16, replacement_ppg=rep)
    # both my (empty TE) and their (empty QB) resulting lineups gain a replacement
    # credit vs empty=0 → both gains shift by exactly the respective replacement.
    assert ef.your_lineup_gain == round(e0.your_lineup_gain + rep["TE"], 2)
    assert ef.their_lineup_gain == round(e0.their_lineup_gain + rep["QB"], 2)


# ---------------------------------------------------------------------------
# END-TO-END on the real seed (guarded) — The Lord punt reprices to ~+14.4
# ---------------------------------------------------------------------------
_WEEKLY_CACHE = Path("data/cache/weekly_pbp_2025.parquet")


@pytest.mark.skipif(
    not _WEEKLY_CACHE.exists(),
    reason="real 2025 per-week data not on disk (CI) — synthetic tests cover the fix",
)
async def test_the_lord_punt_reprices_higher_on_real_seed():
    from backend.database import AsyncSessionLocal
    from backend.services.trade.trade_demo_source import seed_demo_league
    from backend.services.trade.value_engine import evaluate_league
    from backend.services.trade.trade_proposals import evaluate_edge_band, _lineup_roster

    try:
        async with AsyncSessionLocal() as db:
            src = await seed_demo_league(db)
    except Exception as exc:
        pytest.skip(f"demo DB unavailable: {exc}")

    state = src.get_league_state()
    vals = evaluate_league(state, src.weekly_usage, priors=src.priors)
    rep = replacement_ppg_by_position(vals)
    lord = next(t for t in state.teams if t.team_name == "The Lord")
    js = next(t for t in state.teams if t.team_name == "Joe Shiesty")

    def pid(t, nm):
        return next(rp.canonical_player_id for rp in t.roster if rp.name == nm)

    warren = pid(lord, "Tyler Warren")       # The Lord's only TE
    give, get = [warren], [pid(js, "Zay Flowers"), pid(js, "Terry McLaurin")]
    lp_l, lp_j = _lineup_roster(lord, vals), _lineup_roster(js, vals)
    old = evaluate_edge_band(lp_l, lp_j, give, get, roster_limit=16)
    new = evaluate_edge_band(lp_l, lp_j, give, get, roster_limit=16, replacement_ppg=rep)
    assert new.your_lineup_gain > old.your_lineup_gain     # empty=0 understated → floor raises it
    assert new.your_lineup_gain > 7.8                       # was +7.8; now higher (honest)
