"""
Tests for the deterministic pre-draft availability discount (engines/availability.py).
"""
from __future__ import annotations

import pytest

from backend.engines.availability import (
    SEASON_GAMES,
    Cause,
    compute_availability,
    designation_from_sleeper,
)


# ---------------------------------------------------------------------------
# no discount for day-to-day / healthy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("d", ["q", "d", "o", "active", None, ""])
def test_day_to_day_and_healthy_not_discounted(d):
    r = compute_availability(d)
    assert r.factor == 1.0 and r.games_missed == 0 and r.cause is None


def test_unmapped_designation_loud_warns_and_no_discount(caplog):
    with caplog.at_level("WARNING"):
        r = compute_availability("mystery-designation")
    assert r.factor == 1.0
    assert any("unmapped designation" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# WEIGHTED > STRAIGHT — the Rice logic (convex; played weeks retain value)
# ---------------------------------------------------------------------------
def test_weighted_proration_beats_straight_for_partial_season_stud():
    # 6 games missed (a ~6-week suspension). Straight would keep 11/17 = 64.7%.
    r = compute_availability("suspension", weeks_out=6)
    straight = (SEASON_GAMES - 6) / SEASON_GAMES
    assert r.games_missed == 6
    assert r.factor > straight                 # retains MORE than straight proration
    assert r.factor > 0.67                     # meaningfully above straight-67%
    # convexity sanity: discount is less than the raw missed fraction.
    assert (1 - r.factor) < 6 / SEASON_GAMES


def test_heavy_absence_still_heavily_discounted():
    r = compute_availability("suspension", weeks_out=12)
    assert r.factor < 0.60                      # 12/17 missed → still big discount
    assert r.factor > 0.0


# ---------------------------------------------------------------------------
# CAUSE-AWARE — suspension (clean) vs injury (ramp haircut), SAME games missed
# ---------------------------------------------------------------------------
def test_suspension_vs_injury_same_games_missed_differ():
    susp = compute_availability("suspension", weeks_out=6)
    inj = compute_availability("pup", weeks_out=6)          # PUP = injury cause
    assert susp.games_missed == inj.games_missed == 6
    assert susp.cause is Cause.SUSPENSION and inj.cause is Cause.INJURY
    # Injury takes an extra small return haircut → strictly lower factor, same absence.
    assert inj.factor < susp.factor
    # ...but the haircut is SMALL (no double-penalty with the risk term).
    assert (susp.factor - inj.factor) / susp.factor < 0.10


# ---------------------------------------------------------------------------
# designation defaults + weeks_out override
# ---------------------------------------------------------------------------
def test_designation_defaults_when_no_weeks_out():
    assert compute_availability("pup").games_missed == 6
    assert compute_availability("ir_long").games_missed == 13
    assert compute_availability("suspension").games_missed == 4


def test_weeks_out_overrides_default_and_clamps():
    assert compute_availability("suspension", weeks_out=2).games_missed == 2
    assert compute_availability("ir_long", weeks_out=99).games_missed == SEASON_GAMES  # clamp
    assert compute_availability("pup", weeks_out=0).factor == 1.0                       # 0 → no discount


# ---------------------------------------------------------------------------
# Sleeper structured status → designation
# ---------------------------------------------------------------------------
def test_designation_from_sleeper_distinguishes_pup_ir_suspension():
    assert designation_from_sleeper("Physically Unable to Perform", None) == "pup"
    assert designation_from_sleeper("Injured Reserve", "IR") == "ir_long"
    assert designation_from_sleeper("Suspended", None) == "suspension"
    # Day-to-day / rostered → no structured absence.
    assert designation_from_sleeper("Active", "Questionable") is None
    assert designation_from_sleeper("Inactive", None) is None
