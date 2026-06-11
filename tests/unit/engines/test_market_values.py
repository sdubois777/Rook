"""Tests for the pure helpers in backend/engines/market_values.py.

sync_market_values / snapshot / seed functions are I/O orchestration
over scraper + DB and are exercised by integration paths; the
confidence derivation is pure logic and tested here.
"""
from __future__ import annotations

from backend.engines.market_values import _compute_confidence


def test_confidence_missing_min_max_defaults_to_medium():
    """Sources without min/max spread (DraftWizard) report medium confidence."""
    assert _compute_confidence({"avg_value": 30}) == "medium"


def test_confidence_zero_average_is_low():
    """A non-positive average value can't support any confidence."""
    data = {"avg_value": 0, "min_value": 0, "max_value": 5}
    assert _compute_confidence(data) == "low"


def test_confidence_tight_spread_is_high():
    """Experts agreeing within 30% of average means high confidence."""
    data = {"avg_value": 40, "min_value": 36, "max_value": 44}  # spread 20%
    assert _compute_confidence(data) == "high"


def test_confidence_moderate_spread_is_medium():
    """A 30-60% spread maps to medium confidence."""
    data = {"avg_value": 40, "min_value": 32, "max_value": 52}  # spread 50%
    assert _compute_confidence(data) == "medium"


def test_confidence_wide_spread_is_low():
    """Experts disagreeing by more than 60% of average means low confidence."""
    data = {"avg_value": 40, "min_value": 20, "max_value": 60}  # spread 100%
    assert _compute_confidence(data) == "low"
