"""The board's dollar column, and the budget gate on the act-now flags.

TWO QUANTITIES that must not be conflated:

  ``value_gap``          DOLLARS -- ai_bid_ceiling - market. The auction question: what
                         we would bid versus what he will cost.
  ``signal_conviction``  The standardised price-curve residual. Drives the judgements.

``value_gap`` briefly held the curve's ``dollar_edge``, which is only guaranteed in its
SIGN. Its magnitude is an artifact of the clamp inside ``implied_price``: 12 of 155
priced players sat exactly at ``price_cap``, so their printed "gap" was
``price_cap - price`` -- a pure function of price, ranking elite players by cheapness.

The budget gate exists because conviction carries no budget constraint. A player can be
genuinely underpriced for his production AND cost more than the pool allocates to him,
so PAY UP could land next to a negative dollar gap: "pay up" beside "we would not pay
that". Measured: 3 of 13 PAY UP players.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from backend.engines.signal_basis import PriceCurve, fit_price_curve
from backend.engines.valuation import apply_budget_gate, compute_value_gap_from_player


class _P:
    """Minimal duck-typed player — compute_value_gap_from_player reads attributes."""

    def __init__(self, ceiling=None, market=None, recommended=None, baseline=None):
        self.ai_bid_ceiling = ceiling
        self.market_value_fantasypros = market
        self.recommended_bid_ceiling = recommended
        self.baseline_value = baseline


# ===========================================================================
# The dollar column
# ===========================================================================

def test_gap_is_ceiling_minus_market():
    gap, signal = compute_value_gap_from_player(_P(ceiling=55, market=66))
    assert gap == Decimal("-11")
    assert signal == "market_overvalues"


def test_gap_is_zero_sum_against_the_market():
    """The property that makes it readable as money: across the board our ceilings and
    the market's prices are the same pool, so the column nets to roughly zero rather
    than being positive nearly everywhere."""
    board = [(55, 66), (44, 61), (30, 58), (49, 56), (75, 56), (55, 55), (55, 51),
             (40, 39), (38, 39), (19, 39)]
    gaps = [float(compute_value_gap_from_player(_P(ceiling=c, market=m))[0])
            for c, m in board]
    assert sum(gaps) == pytest.approx(-30, abs=40)      # nets near zero, not +400
    assert any(g > 0 for g in gaps) and any(g < 0 for g in gaps)


def test_dollar_edge_saturates_and_must_not_be_the_column():
    """Why the curve's dollar_edge was reverted, reproducing the measured WR board.

    Elite projections invert to a price beyond any real auction, so implied_price clamps
    at 1.5x the priciest player -- and every clamped player's "edge" collapses to
    ``price_cap - price``, which ORDERS THEM BY CHEAPNESS. Built directly rather than
    fitted: the artifact is a property of the clamp, not of any particular fit.
    """
    # The real WR curve shape: price_cap $99 (1.5 x the $66 top price).
    curve = PriceCurve(intercept=100.0, slope=50.0, resid_sd=20.0, n=61, price_cap=99.0)
    # Four elite WRs, all projecting past what $99 buys -> all pinned at the cap.
    board = [("Nacua", 340.0, 66.0), ("Smith-Njigba", 345.0, 61.0),
             ("Chase", 350.0, 56.0), ("St. Brown", 355.0, 51.0)]

    for _n, pts, price in board:
        assert curve.implied_price(pts) == pytest.approx(99.0)

    # ...so the printed "gap" is price_cap - price and nothing else. These are the exact
    # figures that shipped to the board.
    edges = {n: curve.dollar_edge(pts, price) for n, pts, price in board}
    assert edges == pytest.approx(
        {"Nacua": 33.0, "Smith-Njigba": 38.0, "Chase": 43.0, "St. Brown": 48.0})

    # The cheapest of the four shows the LARGEST "value" despite projecting no better
    # relative to the others than his price suggests. That is the artifact.
    by_price = sorted(board, key=lambda r: r[2])
    assert [edges[n] for n, _, _ in by_price] == sorted(
        (edges[n] for n, _, _ in by_price), reverse=True)

    # ...while the SIGN it was actually designed for does still hold.
    for _n, pts, price in board:
        assert (curve.dollar_edge(pts, price) > 0) == (curve.conviction(pts, price) > 0)


def test_a_curve_that_does_not_saturate_still_agrees_in_sign():
    """Guard against over-claiming: the saturation is specific to elite extrapolation.
    A fitted curve over an ordinary board agrees in sign everywhere."""
    prices = [66, 61, 56, 51, 39, 30, 22, 15, 9, 4, 2, 1]
    points = [340, 330, 325, 315, 280, 250, 225, 200, 170, 130, 110, 90]
    curve = fit_price_curve(points, prices)
    assert curve is not None
    for p, pr in zip(points, prices):
        edge, conv = curve.dollar_edge(p, pr), curve.conviction(p, pr)
        if edge is not None and conv is not None and abs(conv) > 1e-9:
            assert (edge > 0) == (conv > 0)


# ===========================================================================
# The budget gate
# ===========================================================================

def test_pay_up_is_suppressed_when_we_would_not_pay_market():
    """The shipped contradiction: PAY UP on Puka Nacua at a $55 ceiling vs a $66 market."""
    pay_up, nomination = apply_budget_gate(True, False, ai_bid_ceiling=55, market=66)
    assert pay_up is False
    assert nomination is False


def test_pay_up_survives_when_our_ceiling_clears_the_market():
    pay_up, _ = apply_budget_gate(True, False, ai_bid_ceiling=75, market=56)
    assert pay_up is True


def test_pay_up_survives_at_exactly_market():
    """Equal is not a contradiction — we would pay it."""
    pay_up, _ = apply_budget_gate(True, False, ai_bid_ceiling=55, market=55)
    assert pay_up is True


def test_nomination_is_gated_symmetrically():
    """The mirror: 'nominate him, the room overpays' on a player we would outbid. Zero
    occurrences on the board this was written against, gated so it stays that way."""
    _, nomination = apply_budget_gate(False, True, ai_bid_ceiling=40, market=30)
    assert nomination is False
    _, nomination = apply_budget_gate(False, True, ai_bid_ceiling=20, market=30)
    assert nomination is True


def test_gate_never_invents_a_flag():
    """It only ever suppresses. A False in is always a False out."""
    for ceiling in (1, 30, 80):
        for market in (1, 30, 80):
            assert apply_budget_gate(False, False, ceiling, market) == (False, False)


def test_missing_data_gates_nothing():
    """With no ceiling or no market there is no contradiction to resolve, so the flags
    pass through rather than being silently dropped."""
    assert apply_budget_gate(True, False, None, 50) == (True, False)
    assert apply_budget_gate(True, False, 50, None) == (True, False)
    assert apply_budget_gate(False, True, None, None) == (False, True)


def test_gate_makes_the_badge_agree_with_the_column_by_construction():
    """The invariant the board needs: a PAY UP row can never show a negative gap, and a
    NOMINATE row can never show a positive one."""
    for ceiling in range(1, 81, 7):
        for market in range(1, 81, 7):
            pay_up, nomination = apply_budget_gate(True, True, ceiling, market)
            gap = float(compute_value_gap_from_player(
                _P(ceiling=ceiling, market=market))[0])
            if pay_up:
                assert gap >= 0
            if nomination:
                assert gap <= 0
