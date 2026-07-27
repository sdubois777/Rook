"""Clearing a player the valuation pass declined to value.

A GHOST is a row with no anchor, no tier and no gap, but a stale `ai_bid_ceiling` left
over from an earlier run. The board still renders that price, and once positional budgets
landed, `apply_board_budgets` still funded it out of a real position's pool. Measured on
production: 20 ghosts holding $30, and 56 holding $123 before the profile backfill.

Two causes, both fixed here:
  1. the clear only covered the MATH fields — the model's auction opinion, the
     market-relative judgement and the whole snake surface survived;
  2. it was gated on `baseline_value is not None`, so an already-half-cleared row could
     never be repaired — the gate saw a null baseline and skipped it forever.
"""
from __future__ import annotations

from decimal import Decimal

from backend.engines.valuation import _UNVALUED_FIELDS, clear_player_valuation


class _P:
    """A player row carrying a full valuation surface."""

    def __init__(self, **overrides):
        self.name = "Ghost"
        self.position = "TE"
        self.tier = 1
        self.adjusted_points = Decimal("220.0")
        self.baseline_value = Decimal("15.90")
        self.ceiling_value = Decimal("24.00")
        self.floor_value = Decimal("8.00")
        self.risk_adjusted_value = Decimal("15.00")
        self.recommended_bid_ceiling = Decimal("15.90")
        self.let_go_threshold = Decimal("19.08")
        self.elite_anchor_weight = Decimal("0.30")
        self.positional_scarcity_modifier = Decimal("1.00")
        self.ai_bid_ceiling = 16
        self.ai_confidence_floor = 12
        self.ai_confidence_ceiling = 20
        self.auction_note = "Volume tight end in a pass-heavy offense."
        self.value_gap = Decimal("-15.00")
        self.value_gap_signal = "market_overvalues"
        self.value_assessment = "elite_value"
        self.signal_conviction = 2.2
        self.pay_up_flag = True
        self.nomination_target_flag = False
        self.adp_ai = Decimal("54.0")
        self.adp_rank = 48
        self.adp_diff = Decimal("6.0")
        self.snake_flag = "VALUE"
        self.data_confidence = "high"
        for k, v in overrides.items():
            setattr(self, k, v)


def test_clearing_strips_every_valuation_field():
    p = _P()
    assert clear_player_valuation(p) is True
    for field, unvalued in _UNVALUED_FIELDS.items():
        assert getattr(p, field) == unvalued, f"{field} survived the clear"


def test_the_dollar_price_the_board_shows_is_cleared():
    """THE GHOST. ai_bid_ceiling is the column the draft board renders and the column
    positional budget enforcement funds. Clearing everything else and leaving this one is
    what made ghosts invisible."""
    p = _P()
    clear_player_valuation(p)
    assert p.ai_bid_ceiling is None


def test_the_snake_surface_is_cleared_too():
    """A cleared player must leave the snake board as well as the auction board —
    otherwise he keeps a rank and a VALUE flag with no valuation behind them."""
    p = _P()
    clear_player_valuation(p)
    assert (p.adp_ai, p.adp_rank, p.adp_diff, p.snake_flag) == (None, None, None, None)


def test_an_already_half_cleared_ghost_is_repaired():
    """THE GATE BUG. The old code only cleared `if baseline_value is not None`, which is
    exactly false for a ghost — so the rows that most needed clearing were the ones it
    skipped, permanently."""
    ghost = _P(
        tier=None, adjusted_points=None, baseline_value=None,
        risk_adjusted_value=None, recommended_bid_ceiling=None,
        let_go_threshold=None, elite_anchor_weight=None,
        positional_scarcity_modifier=None, value_gap=None, value_gap_signal=None,
        data_confidence="low",
    )
    assert ghost.baseline_value is None      # the old gate would stop here
    assert clear_player_valuation(ghost) is True
    assert ghost.ai_bid_ceiling is None
    assert ghost.auction_note is None
    assert ghost.adp_rank is None


def test_clearing_is_idempotent():
    p = _P()
    assert clear_player_valuation(p) is True
    assert clear_player_valuation(p) is False    # nothing left to change
    assert clear_player_valuation(p) is False


def test_not_null_flags_clear_to_false_not_none():
    """pay_up_flag / nomination_target_flag are NOT NULL columns with a False default —
    writing None would violate the constraint on flush."""
    p = _P(pay_up_flag=True, nomination_target_flag=True)
    clear_player_valuation(p)
    assert p.pay_up_flag is False
    assert p.nomination_target_flag is False


def test_confidence_drops_to_low():
    p = _P(data_confidence="high")
    clear_player_valuation(p)
    assert p.data_confidence == "low"


def test_a_row_with_only_a_stale_confidence_still_converges():
    """Degenerate case: everything already cleared except data_confidence. One pass fixes
    it, the next is a no-op."""
    p = _P(**{f: v for f, v in _UNVALUED_FIELDS.items() if f != "data_confidence"})
    p.data_confidence = "high"
    assert clear_player_valuation(p) is True
    assert p.data_confidence == "low"
    assert clear_player_valuation(p) is False
