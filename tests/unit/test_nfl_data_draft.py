"""
tests/unit/test_nfl_data_draft.py

Unit tests for draft capital functions added to backend/integrations/nfl_data.py.
Required by stage-02-data-ingestion.md spec.
"""
from __future__ import annotations

import pytest

from backend.integrations.nfl_data import get_draft_capital_value, get_capital_signal


def test_draft_capital_value_pick_1_is_100():
    """Pick 1 overall = 100."""
    assert get_draft_capital_value(1, 1) == 100.0


def test_draft_capital_value_decreases_with_pick_number():
    """Later picks always produce lower (or equal) capital values."""
    val_1  = get_draft_capital_value(1, 1)
    val_32 = get_draft_capital_value(1, 32)
    val_64 = get_draft_capital_value(2, 64)
    val_128 = get_draft_capital_value(4, 128)
    assert val_1 > val_32 >= val_64 >= val_128


def test_draft_capital_value_pick_32_in_round1_range():
    """Pick 32 (end of round 1) has a meaningful value > 40."""
    val = get_draft_capital_value(1, 32)
    assert val > 40


def test_draft_capital_signal_round1_is_high():
    """Round 1 picks produce 'high' capital signal."""
    val = get_draft_capital_value(1, 5)
    sig = get_capital_signal(val)
    assert sig == "high"


def test_draft_capital_signal_early_round2_is_medium():
    """Early round 2 picks (overall ~33-45) produce 'medium' capital signal."""
    val = get_draft_capital_value(2, 40)
    sig = get_capital_signal(val)
    assert sig == "medium", f"pick 40 gave {val:.1f} ({sig})"


def test_draft_capital_signal_round6_is_low():
    """Round 6-7 picks produce 'low' capital signal."""
    val = get_draft_capital_value(6, 180)
    sig = get_capital_signal(val)
    assert sig == "low"


def test_draft_capital_value_always_positive():
    """No pick should produce a zero or negative capital value."""
    for pick in [1, 32, 64, 128, 200, 256]:
        val = get_draft_capital_value(7, pick)
        assert val > 0, f"pick {pick} gave non-positive value {val}"


def test_capital_signal_thresholds():
    """Verify the exact boundary conditions."""
    assert get_capital_signal(70.0) == "high"
    assert get_capital_signal(69.9) == "medium"
    assert get_capital_signal(40.0) == "medium"
    assert get_capital_signal(39.9) == "low"
    assert get_capital_signal(1.0)  == "low"
