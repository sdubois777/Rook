"""Canonical injury-status normalization — the single vocab mapping point for the
badge. Sleeper (Questionable/Out/IR/DNR/NA) + nflverse (Doubtful) fold to
{Q, D, O, IR}; non-injury statuses → None; unknown strings LOUD-WARN, never vanish."""
from __future__ import annotations

import logging

import pytest

from backend.utils.injury_status import CANONICAL, to_canonical


@pytest.mark.parametrize("raw,code", [
    ("Questionable", "Q"), ("questionable", "Q"), ("Q", "Q"),
    ("Doubtful", "D"),                      # nflverse vocab (Sleeper folds into Q)
    ("Out", "O"),
    ("IR", "IR"), ("Injured Reserve", "IR"), ("PUP", "IR"),  # multi-week → IR bucket
])
def test_maps_injury_designations_to_canonical(raw, code):
    assert to_canonical(raw) == code
    assert code in CANONICAL


@pytest.mark.parametrize("raw", ["DNR", "NA", "N/A", "Sus", "Suspension", "Active", "", None])
def test_non_injury_statuses_map_to_none_no_warn(raw, caplog):
    with caplog.at_level(logging.WARNING):
        assert to_canonical(raw) is None
    assert "unrecognized" not in caplog.text     # known non-injury → silent None, not a warn


def test_unrecognized_string_loud_warns_not_silent(caplog):
    with caplog.at_level(logging.WARNING):
        out = to_canonical("Flesh Wound")
    assert out is None                            # no badge...
    assert "unrecognized designation" in caplog.text and "Flesh Wound" in caplog.text  # ...but LOUD
