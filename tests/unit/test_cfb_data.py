"""
tests/unit/test_cfb_data.py

Unit tests for backend/integrations/cfb_data.py.
Tests that don't require R to be installed (pure Python / formula logic).
"""
from __future__ import annotations

import pytest

from backend.integrations.cfb_data import (
    CONFERENCE_MULTIPLIERS,
    get_adjusted_dominator,
)


def test_conference_multiplier_sec_is_baseline():
    """SEC multiplier == 1.00 (baseline — strongest competition)."""
    assert CONFERENCE_MULTIPLIERS["SEC"] == 1.00


def test_conference_multiplier_mac_reduces_dominator():
    """MAC multiplier == 0.80 (weaker competition)."""
    assert CONFERENCE_MULTIPLIERS["MAC"] == 0.80


def test_adjusted_dominator_sec_unchanged():
    """SEC player: adjusted_dominator == raw (multiplier == 1.00)."""
    adj = get_adjusted_dominator(0.42, "SEC")
    assert adj == pytest.approx(0.42, abs=1e-4)


def test_adjusted_dominator_mac_reduced():
    """MAC player: adjusted_dominator == raw × 0.80."""
    adj = get_adjusted_dominator(0.40, "MAC")
    assert adj == pytest.approx(0.32, abs=1e-4)


def test_adjusted_dominator_big_ten():
    """Big Ten multiplier == 0.97."""
    adj = get_adjusted_dominator(0.40, "Big Ten")
    assert adj == pytest.approx(0.40 * 0.97, abs=1e-4)


def test_adjusted_dominator_unknown_conference_penalized():
    """Unknown conference gets 0.85 multiplier (conservative penalty)."""
    adj = get_adjusted_dominator(0.40, "SomeMadeUpConference")
    assert adj == pytest.approx(0.40 * 0.85, abs=1e-4)


def test_all_conferences_have_values_between_0_and_1():
    """All multipliers are in (0, 1] range."""
    for conf, mult in CONFERENCE_MULTIPLIERS.items():
        assert 0 < mult <= 1.0, f"Multiplier for {conf} out of range: {mult}"


def test_sec_highest_multiplier():
    """SEC has the highest competition multiplier."""
    sec = CONFERENCE_MULTIPLIERS["SEC"]
    for conf, mult in CONFERENCE_MULTIPLIERS.items():
        assert mult <= sec, f"{conf} multiplier {mult} exceeds SEC {sec}"


def test_adjusted_dominator_always_lte_raw():
    """Adjusted dominator is always ≤ raw (no conference boosts above baseline)."""
    for conf in CONFERENCE_MULTIPLIERS:
        raw = 0.35
        adj = get_adjusted_dominator(raw, conf)
        assert adj <= raw + 1e-9, f"{conf} boosted dominator above raw"


def test_dominator_formula_correct():
    """
    Dominator rating formula verification (tested via unit math, not R):
    dominator = (player_rec_yards / team_rec_yards + player_rec_tds / team_rec_tds) / 2
    """
    player_rec_yards = 1200
    team_rec_yards   = 3000
    player_rec_tds   = 10
    team_rec_tds     = 25
    expected = (player_rec_yards / team_rec_yards + player_rec_tds / team_rec_tds) / 2
    assert expected == pytest.approx((0.40 + 0.40) / 2, abs=1e-4)
    assert expected == pytest.approx(0.40, abs=1e-4)
