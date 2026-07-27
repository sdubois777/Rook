"""_apply_hybrid_rails — the non-market leash on the model's raw ai_bid_ceiling.

Two properties this pins, both regressions:

1. EVERY POSITION IS RAILED. The rails used to be scoped to the reception positions
   (RB/WR/TE), an exclusion QB inherited from `_FORMAT_INVARIANT_POSITIONS` — a set that
   exists because QB points are identical in every scoring format. That is true and
   unrelated to budgets. Unrailed, QB was written from the model's number with only the
   $50 cap in the way: C.J. Stroud and Matthew Stafford landed at $32 against a $1 math
   anchor, well under the cap, so nothing caught it.

2. THE RAILS ARE PER POSITION. Rail (2) rescales to the anchor aggregate and rail (3)
   caps each tier at the better tier's max. Both are only meaningful inside one position:
   over a merged pool, one position absorbs another's slack, and a tier-1 QB gets compared
   to a tier-2 RB.
"""
from __future__ import annotations

from backend.agents.valuation_agent import _POS_MAX_BID, _apply_hybrid_rails


def _row(name, position, anchor, ai_raw, tier=1):
    return {"name": name, "position": position, "anchor": anchor,
            "ai_raw": ai_raw, "tier": tier}


def test_qb_is_railed_to_its_anchor_not_just_capped():
    """The exact shape of the shipped bug: a $1-anchor QB the model priced at $32. Under
    the $50 cap, so a clamp-only path wrote it through untouched."""
    out = _apply_hybrid_rails([
        _row("Anchored QB", "QB", anchor=1.0, ai_raw=32, tier=5),
        _row("Real QB", "QB", anchor=30.0, ai_raw=32, tier=1),
    ])
    assert out["Anchored QB"] < 32
    assert out["Anchored QB"] <= _POS_MAX_BID["QB"]
    # ...and the position still sums to its own anchor aggregate, not to 2x it.
    assert sum(out.values()) == 31          # $1 + $30 anchors, rounded


def test_leash_holds_in_both_directions():
    """+/-25% off the math anchor. A lone player is his own aggregate, so rail (2) is a
    no-op and the leash is what shows."""
    high = _apply_hybrid_rails([_row("High", "WR", anchor=20.0, ai_raw=100)])
    low = _apply_hybrid_rails([_row("Low", "WR", anchor=20.0, ai_raw=1)])
    assert high["High"] == 20               # leashed to 25, rescaled back to the anchor
    assert low["Low"] == 20


def test_rails_are_scoped_per_position():
    """A wildly over-priced WR pool must not drag the QB pool's dollars, and vice versa.
    Each position rescales to ITS OWN anchor aggregate."""
    out = _apply_hybrid_rails([
        _row("WR1", "WR", anchor=40.0, ai_raw=200),
        _row("WR2", "WR", anchor=20.0, ai_raw=200),
        _row("QB1", "QB", anchor=30.0, ai_raw=30),
        _row("QB2", "QB", anchor=10.0, ai_raw=10),
    ])
    assert out["WR1"] + out["WR2"] == 60    # the WR anchor aggregate
    assert out["QB1"] + out["QB2"] == 40    # the QB anchor aggregate, untouched by WR


def test_tier_ordinal_does_not_compare_across_positions():
    """A tier-2 RB must be bounded by the best tier-1 RB, never by the best tier-1 QB —
    which is what a merged pool did."""
    out = _apply_hybrid_rails([
        _row("QB1", "QB", anchor=5.0, ai_raw=5, tier=1),      # a cheap tier-1 QB
        _row("RB1", "RB", anchor=60.0, ai_raw=60, tier=1),
        _row("RB2", "RB", anchor=50.0, ai_raw=50, tier=2),
    ])
    assert out["RB2"] > out["QB1"]          # the cheap QB does not cap the RB pool
    assert out["RB2"] <= out["RB1"]         # but the better RB tier still does


def test_position_cap_is_the_last_word():
    out = _apply_hybrid_rails([
        _row("Huge TE", "TE", anchor=200.0, ai_raw=200),
        _row("Huge K", "K", anchor=50.0, ai_raw=50),
    ])
    assert out["Huge TE"] == _POS_MAX_BID["TE"]
    assert out["Huge K"] == _POS_MAX_BID["K"]


def test_floor_is_a_dollar():
    out = _apply_hybrid_rails([_row("Cheap", "WR", anchor=0.0, ai_raw=0)])
    assert out["Cheap"] == 1


def test_missing_model_value_falls_back_to_the_anchor():
    """A row the model never returned a ceiling for prices at its math anchor rather than
    at $0 — the anchor is the honest default, not a hole."""
    out = _apply_hybrid_rails([_row("Silent", "RB", anchor=25.0, ai_raw=None)])
    assert out["Silent"] == 25


def test_empty_is_a_noop():
    assert _apply_hybrid_rails([]) == {}
