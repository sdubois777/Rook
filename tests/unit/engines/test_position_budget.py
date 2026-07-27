"""Positional budget enforcement — every position lands on its budget share.

The bug this pins: `ai_bid_ceiling` (the number the draft board shows) is written by the
LLM stage, and the only sum-to-budget rail was scoped to the RECEPTION positions
(RB/WR/TE). QB inherited that exclusion from `_FORMAT_INVARIANT_POSITIONS` — which exists
because QB POINTS do not change by scoring format. Format-invariance is real; budget
enforcement is not format math and must apply to every position. Measured on the local
board before this test existed: QB realized 15.0% against an 11.1% target, RB missed by
10.1 points, and the board totalled $3556 against a $2220 pool.

BUDGETED POPULATION — read this before changing the assertions. The budget is enforced
over each position's DRAFTABLE POOL (`get_draftable_pool_sizes`, the same top-N the
replacement level and the z-tiers are already built on), not over every priced row. A
12-team league buys ~150 skill players; the board prices ~673. Charging the $1 depth tail
against the pool is a category error AND arithmetically degenerate: TE has 146 priced rows
and a 7.6% share, so a whole-population constraint leaves $23 above the $1 floor for all of
them and prices the best TE in football at $3. Same for QB ($89 across 30 above-floor QBs →
a $12 Josh Allen). The tail is scaled by the SAME factor and floored at $1, so ordering is
continuous across the pool boundary and no visible player is dropped.
"""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engines.valuation import (
    LEAGUE_SKILL_DOLLAR_POOL,
    MAX_REALISTIC_BID,
    POSITION_BUDGET_SHARE,
    _format_budget_shares,
    apply_board_budgets,
    enforce_ai_ceiling_budgets,
    enforce_position_budget,
    get_draftable_pool_sizes,
)

POOL = float(LEAGUE_SKILL_DOLLAR_POOL)
POSITIONS = ("QB", "RB", "WR", "TE")
FORMATS = ("ppr", "half_ppr", "standard")

# Row counts from the real local board (the population actually priced).
_BOARD_SHAPE = {"QB": 95, "RB": 155, "WR": 277, "TE": 146}
# Per-position aggregate PAR context for _format_budget_shares. Standard shrinks the
# reception positions and holds RB/QB — the same shape test_scoring.py uses.
_PPR_PAR = {"QB": 300.0, "RB": 1000.0, "WR": 900.0, "TE": 300.0}
_FMT_PAR = {
    "ppr": _PPR_PAR,
    "half_ppr": {"QB": 300.0, "RB": 975.0, "WR": 750.0, "TE": 250.0},
    "standard": {"QB": 300.0, "RB": 950.0, "WR": 600.0, "TE": 200.0},
}


def _synthetic_board() -> list[dict]:
    """A hostile, unenforced board shaped like the LLM's actual output.

    Deliberately breaks every invariant the enforcement must restore: the total runs far
    over the pool, the top of each position sits ABOVE `MAX_REALISTIC_BID`, the positional
    mix is wrong (QB and TE over-allocated exactly as measured), and there is a long
    near-$1 tail. Deterministic — no randomness, no DB.
    """
    # top dollar, decay constant. QB/TE tops exceed their caps ($50 / $45) so the clamp
    # path is exercised, not just the scale path.
    shape = {"QB": (90.0, 14.0), "RB": (110.0, 22.0), "WR": (95.0, 30.0), "TE": (60.0, 12.0)}
    rows: list[dict] = []
    for pos, n in _BOARD_SHAPE.items():
        top, decay = shape[pos]
        for i in range(n):
            rows.append({
                "key": f"{pos}{i}",
                "position": pos,
                "value": max(1.0, round(top * math.exp(-i / decay))),
            })
    return rows


def _by_position(rows: list[dict], out: dict) -> dict[str, list[int]]:
    """Enforced values grouped by position, DESC — pool members first."""
    grouped: dict[str, list[int]] = {}
    for r in rows:
        grouped.setdefault(r["position"], []).append(out[r["key"]])
    for pos in grouped:
        grouped[pos].sort(reverse=True)
    return grouped


# ===========================================================================
# The headline test — section 4 step 1 of the handoff
# ===========================================================================

@pytest.mark.parametrize("scoring_format", FORMATS)
def test_every_position_lands_on_its_budget(scoring_format):
    """Each position's realized share is within 2 points of its target, the draftable
    pool totals the skill pool +/-2%, and every value is in [$1, MAX_REALISTIC_BID]."""
    rows = _synthetic_board()
    shares = _format_budget_shares(scoring_format, _PPR_PAR, _FMT_PAR[scoring_format])
    pool_sizes = get_draftable_pool_sizes()

    out = apply_board_budgets(rows, POOL, shares, pool_sizes)
    grouped = _by_position(rows, out)

    spent = {pos: sum(v[:pool_sizes[pos]]) for pos, v in grouped.items()}
    total = sum(spent.values())

    # (1) total lands on the pool
    assert total == pytest.approx(POOL, rel=0.02), (
        f"{scoring_format}: draftable pool totals ${total} against a ${POOL:.0f} pool"
    )

    # (2) every position within 2 points of its share — QB INCLUDED
    share_sum = sum(shares[p] for p in POSITIONS)
    for pos in POSITIONS:
        realized = spent[pos] / total * 100
        target = shares[pos] / share_sum * 100
        assert abs(realized - target) <= 2.0, (
            f"{scoring_format} {pos}: realized {realized:.1f}% vs target {target:.1f}% "
            f"({realized - target:+.1f} points)"
        )

    # (3) bounds hold for EVERY row, tail included
    for pos, values in grouped.items():
        assert min(values) >= 1, f"{scoring_format} {pos}: value below $1"
        assert max(values) <= MAX_REALISTIC_BID[pos], (
            f"{scoring_format} {pos}: ${max(values)} exceeds cap ${MAX_REALISTIC_BID[pos]}"
        )


@pytest.mark.parametrize("scoring_format", FORMATS)
def test_enforcement_preserves_within_position_ordering(scoring_format):
    """Enforcement is cross-position allocation only. It must never reorder a position —
    conviction is computed WITHIN position and would be corrupted by a reshuffle."""
    rows = _synthetic_board()
    shares = _format_budget_shares(scoring_format, _PPR_PAR, _FMT_PAR[scoring_format])
    out = apply_board_budgets(rows, POOL, shares, get_draftable_pool_sizes())

    for pos in POSITIONS:
        before = [r for r in rows if r["position"] == pos]
        for a, b in zip(before, before[1:]):
            if a["value"] > b["value"]:
                assert out[a["key"]] >= out[b["key"]], (
                    f"{pos}: {a['key']} (${a['value']}) priced below {b['key']} "
                    f"(${b['value']}) after enforcement"
                )


def test_qb_is_railed_not_merely_clamped():
    """The regression itself. QB was excluded from the only sum-to-budget rail, so an
    unrailed QB board sat far above its share while staying under the $50 cap — the cap
    alone never brought it back."""
    rows = _synthetic_board()
    pool_sizes = get_draftable_pool_sizes()
    qb_before = sorted(
        (r["value"] for r in rows if r["position"] == "QB"), reverse=True
    )[:pool_sizes["QB"]]
    clamped_only = sum(min(v, MAX_REALISTIC_BID["QB"]) for v in qb_before)

    out = apply_board_budgets(rows, POOL, POSITION_BUDGET_SHARE, pool_sizes)
    qb_after = sum(sorted(
        (out[r["key"]] for r in rows if r["position"] == "QB"), reverse=True
    )[:pool_sizes["QB"]])
    target = POSITION_BUDGET_SHARE["QB"] / sum(POSITION_BUDGET_SHARE.values()) * POOL

    assert clamped_only > target * 1.5, "fixture is not hostile enough to show the bug"
    assert qb_after == pytest.approx(target, rel=0.02)


def test_shares_sum_to_one_so_no_pool_is_stranded():
    """POSITION_BUDGET_SHARE summing to 0.90 stranded $222 of the pool by construction."""
    assert sum(POSITION_BUDGET_SHARE.values()) == pytest.approx(1.0)


# ===========================================================================
# enforce_position_budget — the pure water-filling primitive
# ===========================================================================

def test_water_fill_hits_the_target_exactly():
    out = enforce_position_budget([100, 50, 25, 10, 5], target=100, cap=80)
    assert sum(out) == 100
    assert all(v >= 1 for v in out)


def test_water_fill_redistributes_the_capped_residual():
    """Naive scaling fights the clamps: scale-then-clamp loses the dollars the cap took.
    Water-filling hands them to the still-unclamped players instead."""
    out = enforce_position_budget([1000, 10, 10, 10], target=200, cap=80)
    assert out[0] == 80                       # pinned at the cap
    assert sum(out) == 200                    # ...and the other 120 went somewhere
    assert sum(out[1:]) == 120


def test_water_fill_respects_the_floor_when_scaling_down():
    out = enforce_position_budget([100, 2, 2, 2, 2], target=20, cap=80)
    assert min(out) >= 1
    assert sum(out) == 20


def test_water_fill_is_infeasible_below_the_floor_and_says_so():
    """n players cannot cost less than n dollars. The function returns the floor rather
    than inventing sub-$1 money, and the caller sees the overshoot."""
    out = enforce_position_budget([5, 5, 5, 5, 5], target=2, cap=80)
    assert out == [1, 1, 1, 1, 1]


def test_water_fill_scales_up_to_an_underspent_target():
    out = enforce_position_budget([10, 8, 6, 4, 2], target=300, cap=80)
    assert sum(out) == 300
    assert max(out) <= 80


def test_budgeted_subset_carries_the_target_and_the_tail_rides_the_same_scale():
    """The $1 depth tail is priced by the SAME factor as the pool (so ordering stays
    continuous across the boundary) but does not count against the position's budget."""
    values = [100, 80, 60, 40, 20, 10, 5, 2]
    out = enforce_position_budget(values, target=100, cap=80, budgeted=4)
    assert sum(out[:4]) == 100                 # the pool spends exactly the budget
    assert sum(out) > 100                      # the tail still costs its $1s
    assert all(v >= 1 for v in out)
    assert out == sorted(out, reverse=True)    # monotone across the boundary


def test_empty_position_is_a_noop():
    assert enforce_position_budget([], target=100, cap=80) == []


# ===========================================================================
# enforce_ai_ceiling_budgets — the DB pass that must run AFTER valuation_agent
# ===========================================================================

def _mock_session(players):
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=MagicMock(
        scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=players)))
    ))
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _mock_player(pid, position, ceiling):
    p = MagicMock()
    p.id = pid
    p.position = position
    p.ai_bid_ceiling = ceiling
    return p


@pytest.mark.asyncio
async def test_db_pass_writes_ai_bid_ceiling_not_the_math_anchor():
    """The trap this whole change exists to avoid. ai_bid_ceiling is what the board shows
    and what the agent writes; recommended_bid_ceiling is the pool-share math the agent
    then overwrites. Enforcement that lands on the second is invisible."""
    players = [_mock_player(f"qb{i}", "QB", 45) for i in range(20)]
    session = _mock_session(players)

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await enforce_ai_ceiling_budgets()

    assert result["updated"] > 0
    pool_size = get_draftable_pool_sizes()["QB"]
    spent = sum(sorted((p.ai_bid_ceiling for p in players), reverse=True)[:pool_size])
    target = POSITION_BUDGET_SHARE["QB"] / sum(POSITION_BUDGET_SHARE.values()) * POOL
    assert spent == pytest.approx(target, rel=0.02)
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_db_pass_dry_run_writes_nothing():
    players = [_mock_player(f"rb{i}", "RB", 60) for i in range(60)]
    session = _mock_session(players)

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await enforce_ai_ceiling_budgets(dry_run=True)

    assert result["dry_run"] is True
    assert result["updated"] > 0                       # it reports what it WOULD change
    assert all(p.ai_bid_ceiling == 60 for p in players)  # ...and changes nothing
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited()


def test_unbudgeted_positions_are_only_bounded():
    """K/DEF have no budget share — they are $1 streamers valued on a separate static
    path. They must pass through bounded, never rescaled into the skill pool."""
    rows = [
        {"key": "k1", "position": "K", "value": 1},
        {"key": "d1", "position": "DEF", "value": 9},
        {"key": "rb1", "position": "RB", "value": 50},
    ]
    out = apply_board_budgets(rows, POOL, POSITION_BUDGET_SHARE, get_draftable_pool_sizes())
    assert out["k1"] == 1
    assert out["d1"] == MAX_REALISTIC_BID["DEF"]   # bounded by the $2 K/DEF cap


def test_a_missing_position_does_not_hand_its_pool_to_the_others():
    """Shares are renormalised over the WHOLE share set, not over the positions present.
    A board with only QBs on it must still get QB's share of the pool — not all of it."""
    rows = [{"key": f"qb{i}", "position": "QB", "value": 40} for i in range(20)]
    out = apply_board_budgets(rows, POOL, POSITION_BUDGET_SHARE, get_draftable_pool_sizes())
    spent = sum(sorted(out.values(), reverse=True)[:get_draftable_pool_sizes()["QB"]])
    target = POSITION_BUDGET_SHARE["QB"] / sum(POSITION_BUDGET_SHARE.values()) * POOL
    assert spent == pytest.approx(target, rel=0.02)
