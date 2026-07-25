"""Share-based sizing for dependency flags, and rate-based model scoring.

Both exist because of one measured fact: the flags' PRESENCE carries signal (|t| 1.76-1.83,
sign stable in 97-98% of bootstraps) while ``dep_net_impact`` — the magnitude the system
assigns — carries none (t = -0.09). The system knows who is affected and not how much.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.engines.backtest import RATE_MIN_GAMES, score_on_rate
from backend.engines.share_transfer import (
    ARRIVAL_DILUTION,
    ARRIVAL_DILUTION_DEFAULT,
    DEPARTURE_ABSORPTION,
    beneficiary_impact_pct,
    displaced_impact_pct,
    impact_for_flag,
)


# ---------------------------------------------------------------------------
# Departures — the beneficiary side
# ---------------------------------------------------------------------------

def test_bigger_departure_means_bigger_uplift():
    """THE point of the change. A flat weight cannot tell Metcalf's vacated share from
    Tyler Lockett's; this must."""
    small = beneficiary_impact_pct(vacated_share=0.05, incumbent_total_share=0.60)
    big = beneficiary_impact_pct(vacated_share=0.25, incumbent_total_share=0.60)
    assert big > small > 0
    # Uplift is linear in vacated share, so 5x the departure is 5x the uplift.
    assert big == pytest.approx(small * 5, rel=1e-6)


def test_uplift_is_relative_so_it_does_not_depend_on_the_individuals_share():
    """Absorbed share is split in proportion to what each incumbent already had, so the
    RELATIVE uplift is identical across the room — which is why this takes the team
    total, not the individual."""
    v = beneficiary_impact_pct(vacated_share=0.20, incumbent_total_share=0.50)
    assert v == pytest.approx(100 * DEPARTURE_ABSORPTION * 0.20 / 0.50, rel=1e-6)


def test_thinner_remaining_room_concentrates_the_uplift():
    """The same departure into a room with less total share left lifts each survivor more."""
    wide = beneficiary_impact_pct(vacated_share=0.20, incumbent_total_share=0.70)
    thin = beneficiary_impact_pct(vacated_share=0.20, incumbent_total_share=0.35)
    assert thin > wide


def test_absorption_is_partial_not_total():
    """Only ~26% of vacated share stays in the room; the rest goes to signings, rookies
    and scheme change. A model that hands it all to the WR2 is wrong."""
    assert 0 < DEPARTURE_ABSORPTION < 0.5
    v = beneficiary_impact_pct(vacated_share=0.30, incumbent_total_share=0.30)
    assert v < 100.0, "uplift cannot exceed the share that actually moved"


# ---------------------------------------------------------------------------
# Arrivals — the displaced side (the user's addition)
# ---------------------------------------------------------------------------

def test_arrival_hit_is_negative_and_scales():
    small = displaced_impact_pct(arrival_share=0.05, position="WR")
    big = displaced_impact_pct(arrival_share=0.25, position="WR")
    assert big < small < 0
    # abs tolerance, not relative: results are rounded to 2dp for the Numeric(4,2)
    # column, so multiplying the smaller value compounds that rounding.
    assert big == pytest.approx(small * 5, abs=0.05)


def test_arrival_dilution_is_position_specific():
    """Measured: WR -0.655, TE -0.497, RB -0.757. An RB room concedes more than a TE room."""
    a = 0.20
    rb = displaced_impact_pct(a, "RB")
    wr = displaced_impact_pct(a, "WR")
    te = displaced_impact_pct(a, "TE")
    assert rb < wr < te < 0, (rb, wr, te)
    assert ARRIVAL_DILUTION["RB"] > ARRIVAL_DILUTION["WR"] > ARRIVAL_DILUTION["TE"]


def test_unknown_position_uses_the_pooled_coefficient():
    v = displaced_impact_pct(0.20, "FB")
    assert v == pytest.approx(-100 * ARRIVAL_DILUTION_DEFAULT * 0.20, rel=1e-6)
    assert displaced_impact_pct(0.20, None) == v


def test_dilution_is_proportional_so_the_relative_hit_is_share_independent():
    """The flat term measured t = -0.67 (nothing), so every incumbent takes the same
    PERCENTAGE hit regardless of whether he is the WR1 or the WR4. The function
    deliberately takes no individual-share argument — this pins that."""
    import inspect
    params = set(inspect.signature(displaced_impact_pct).parameters)
    assert params == {"arrival_share", "position"}


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"vacated_share": 0, "incumbent_total_share": 0.5},
    {"vacated_share": 0.2, "incumbent_total_share": 0},
    {"vacated_share": None, "incumbent_total_share": 0.5},
    {"vacated_share": 0.2, "incumbent_total_share": None},
])
def test_beneficiary_returns_none_rather_than_a_fabricated_zero(kwargs):
    """None means 'no share-based claim' so the caller keeps the existing estimate.
    Writing 0.0 would silently assert 'this departure is worth nothing'."""
    assert beneficiary_impact_pct(**kwargs) is None


@pytest.mark.parametrize("share", [0, -0.1, None])
def test_displaced_returns_none_without_a_real_arrival(share):
    assert displaced_impact_pct(share, "WR") is None


def test_impacts_are_clamped_to_a_plausible_range():
    """A corrupt share must not overflow Numeric(4,2) or claim '-900% of his value'."""
    assert -60.0 <= displaced_impact_pct(5.0, "RB") <= 0
    assert 0 <= beneficiary_impact_pct(9.0, 0.01) <= 60.0


# ---------------------------------------------------------------------------
# Flag routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag,sign", [
    ("beneficiary", 1), ("contingent", 1), ("displaced", -1), ("committee", -1),
])
def test_flag_routes_to_the_right_direction(flag, sign):
    v = impact_for_flag(
        flag, vacated_share=0.2, incumbent_total_share=0.5,
        arrival_share=0.2, position="WR",
    )
    assert v is not None and np.sign(v) == sign


@pytest.mark.parametrize("flag", ["scheme_fit", "college_trust", "", None, "nonsense"])
def test_judgement_flags_are_left_alone(flag):
    """scheme_fit and college_trust are judgements about a player, not about how a target
    room was redistributed. Inventing a share for them would be fabrication."""
    assert impact_for_flag(
        flag, vacated_share=0.2, incumbent_total_share=0.5, arrival_share=0.2,
    ) is None


# ---------------------------------------------------------------------------
# Rate-based model scoring
# ---------------------------------------------------------------------------

def _frame(n=120, seed=0, injure=False):
    rng = np.random.default_rng(seed)
    price = np.exp(rng.uniform(0, 4.0, n))
    rate = 12 * np.log(price) + rng.normal(0, 25, n) + 120
    games = np.full(n, 17.0)
    if injure:
        games[: n // 3] = rng.integers(4, 10, n // 3)
    return pd.DataFrame({
        "position": rng.choice(["RB", "WR"], n),
        "league_price": price,
        "proj_ppr": rate + rng.normal(0, 25, n),
        "actual_games": games,
        "actual_ppr": rate * games / 17.0,
    })


def test_rate_scoring_returns_accuracy_calls_and_edge():
    acc, n, r = score_on_rate(_frame())
    assert acc is not None and 0 <= acc <= 100
    assert n > 50 and r is not None


def test_rate_scoring_is_unmoved_by_injuries_that_wreck_totals():
    """THE reason this metric exists. Identical per-game quality, but a third of the field
    misses games — the totals-based outcome swings, the rate-based one should not."""
    healthy = score_on_rate(_frame(seed=1, injure=False))[0]
    injured = score_on_rate(_frame(seed=1, injure=True))[0]
    assert abs(healthy - injured) < 12, (healthy, injured)


def test_short_samples_are_excluded_rather_than_extrapolated_17x():
    """Extrapolating a 2-game rate to a full season manufactures noise instead of
    removing it."""
    df = _frame(seed=2)
    df.loc[df.index[:40], "actual_games"] = 2
    _, n, _ = score_on_rate(df)
    assert n <= len(df) - 40
    assert RATE_MIN_GAMES >= 3


def test_rate_scoring_degrades_quietly_on_unusable_input():
    assert score_on_rate(pd.DataFrame()) == (None, 0, None)
    assert score_on_rate(_frame(n=5)) == (None, 0, None)
    assert score_on_rate(pd.DataFrame({"position": ["WR"]})) == (None, 0, None)


def test_zero_games_never_divides_by_zero():
    df = _frame(seed=3)
    df.loc[df.index[:10], "actual_games"] = 0
    acc, n, _ = score_on_rate(df)
    assert acc is not None and n == len(df) - 10
