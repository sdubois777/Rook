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


# ---------------------------------------------------------------------------
# ESPN's real spellings, sampled live from a real league's player list (1,026
# players). ESPN uses SCREAMING_SNAKE, which is why these are pinned here: the
# normalizer fails SAFE but QUIET, so a spelling it does not recognise produces no
# badge and only a log line. That looks identical to the bug being fixed — every
# player reading as healthy — while appearing to work.
# ---------------------------------------------------------------------------
ESPN_OBSERVED = {
    "ACTIVE": None,             # 899 of 1026 — healthy, correctly no badge
    "QUESTIONABLE": "Q",        # 64
    "OUT": "O",                 # 15
    "INJURY_RESERVE": "IR",     # 6 — ESPN's wording for injured reserve
}


def test_espn_observed_designations_all_normalize():
    """Every value ESPN actually emitted, with the code it must produce."""
    for raw, expected in ESPN_OBSERVED.items():
        assert to_canonical(raw) == expected, f"{raw} should map to {expected}"


def test_espn_injured_reserve_is_not_silently_dropped(caplog):
    """The one that was falling through. An unrecognised spelling returns None with
    only a warning, so these six players would have been treated as available and
    seated in the optimal lineup — the same symptom as reading no injury data at
    all, which is exactly what this work exists to fix."""
    with caplog.at_level("WARNING"):
        assert to_canonical("INJURY_RESERVE") == "IR"
    assert not any("unrecognized designation" in m for m in caplog.messages)


def test_espn_doubtful_maps_even_though_the_sample_had_none():
    """No player carried DOUBTFUL in the sample, but it is a real NFL designation
    and ESPN uses the same casing style for it."""
    assert to_canonical("DOUBTFUL") == "D"
