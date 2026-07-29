"""sanity_check_valuations must not go stale when the budget shares move.

It used to warn three times on every production run, and two of those were artifacts:

  * the total was summed over EVERY valued row, including the ~500-row $1 depth tail no
    auction ever buys, so it exceeded the pool by construction and could NEVER pass;
  * the per-position bounds were absolute dollar averages over that tail-dominated
    population — really a restatement of `share x pool / row_count`, calibrated when QB
    was .10 and TE .10, and broken the moment those became .083 and .076.

A check that always warns is a check nobody reads, so every bound now derives from
POSITION_BUDGET_SHARE and the draftable pool.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backend.engines import valuation as V
from backend.engines.valuation import (
    LEAGUE_SKILL_DOLLAR_POOL,
    POSITION_BUDGET_SHARE,
    get_draftable_pool_sizes,
    sanity_check_valuations,
)

POOL = float(LEAGUE_SKILL_DOLLAR_POOL)


def _player(position, value):
    p = MagicMock()
    p.position = position
    p.baseline_value = Decimal(str(value))
    return p


def _board(shares=None, tail_per_pos=120):
    """A board that spends each position's share across its draftable pool, plus a long
    $1 depth tail — the real shape, which the old checks could not tolerate.

    The curve is deliberately GENTLE (top:bottom about 4:1) so no generated value lands
    near MAX_REALISTIC_BID. These tests are about the share bookkeeping; a fixture that
    trips the position-cap check would be testing itself.
    """
    shares = shares or POSITION_BUDGET_SHARE
    sizes = get_draftable_pool_sizes()
    share_sum = sum(shares.values())
    players = []
    for pos, share in shares.items():
        k = sizes[pos]
        target = POOL * share / share_sum
        weights = [1.0 + (k - i) / k * 3.0 for i in range(k)]
        scale = target / sum(weights)
        for w in weights:
            players.append(_player(pos, max(1.0, round(w * scale, 2))))
        players.extend(_player(pos, 1.0) for _ in range(tail_per_pos))
    return players


def test_a_correctly_budgeted_board_is_clean():
    """THE REGRESSION. This board is exactly what the pipeline aims to produce, and the
    old checks warned on it every single run."""
    assert sanity_check_valuations(_board(), POOL) == []


def test_the_dollar_tail_alone_never_trips_the_total():
    """500 rows of $1 depth is normal and is not spend — the total is measured over the
    draftable pool, so lengthening the tail cannot make the board look inflated."""
    assert sanity_check_valuations(_board(tail_per_pos=400), POOL) == []


def test_a_genuinely_inflated_pool_is_still_caught():
    players = [_player("RB", 300) for _ in range(20)]
    warnings = sanity_check_valuations(players, POOL)
    assert any("exceeds pool" in w for w in warnings)


def test_a_position_off_its_share_is_flagged():
    """The failure actually worth hearing about: the math and the budget disagree."""
    shares = {"QB": 0.30, "RB": 0.30, "WR": 0.30, "TE": 0.10}   # QB way over its 8.3%
    warnings = sanity_check_valuations(_board(shares), POOL)
    assert any(w.startswith("QB holds") for w in warnings), warnings


def test_the_bounds_follow_the_shares_rather_than_hardcoded_dollars(monkeypatch):
    """Scale-free: a board built to a DIFFERENT share set is clean once the constant says
    so. A hardcoded bound would flag one of the two."""
    alt = {"QB": 0.16, "RB": 0.30, "WR": 0.39, "TE": 0.15}   # QB ~2x its shipped share
    # Built to `alt` but judged against the shipped shares -> should complain.
    assert sanity_check_valuations(_board(alt), POOL) != []
    # Judged against the shares it was built to -> clean, with no code change.
    monkeypatch.setattr(V, "POSITION_BUDGET_SHARE", alt)
    assert sanity_check_valuations(_board(alt), POOL) == []


def test_position_cap_is_still_enforced():
    players = _board()
    players.append(_player("TE", 999))
    warnings = sanity_check_valuations(players, POOL)
    assert any("exceeds cap" in w for w in warnings)


def test_distribution_check_scales_with_the_pool_not_a_magic_count():
    """Expressed as a fraction of the draftable pool, so a roster-size or shares change
    cannot silently invalidate it."""
    thin = [_player("RB", 1.0) for _ in range(200)]      # nobody above $10
    warnings = sanity_check_valuations(thin, POOL)
    assert any("above $10" in w for w in warnings)
    assert "% of the" in next(w for w in warnings if "above $10" in w)


def test_empty_board_does_not_explode():
    assert isinstance(sanity_check_valuations([], POOL), list)


def test_tolerance_is_a_named_constant_not_a_literal():
    """So the next person tuning it changes one documented number."""
    assert isinstance(V._SANITY_SHARE_TOLERANCE_PTS, float)
    assert 0 < V._SANITY_SHARE_TOLERANCE_PTS < 25
