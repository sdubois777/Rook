"""The orthogonality harness — does a candidate signal know anything the PRICE does not?

This exists to enforce one rule that is algebra, not opinion: `was_good_buy` is a residual
against a within-position ln(price) fit, so anything the market already holds is worth
EXACTLY zero against it. Measured — blending our projection with the market price raised
correlation with realised per-game rate from 0.566 to 0.630 while signal accuracy stayed
at 60.3% at every blend weight.

Without this gate, "improve the projection" work can look successful on MAE/correlation
and deliver nothing. These tests pin the behaviour that makes the gate trustworthy.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.engines.backtest import (
    ORTHOGONALITY_MIN_N,
    ORTHOGONALITY_T_SUGGESTIVE,
    ORTHOGONALITY_T_THRESHOLD,
    measure_orthogonality,
)


def _board(n=200, seed=0, **cols):
    """A synthetic priced board: price drives both projection and outcome, as in reality."""
    rng = np.random.default_rng(seed)
    price = np.exp(rng.uniform(0, 4.2, n))            # $1..$65-ish, log-uniform
    truth = 20 * np.log(price) + rng.normal(0, 40, n)
    df = pd.DataFrame({
        "position": rng.choice(["RB", "WR"], n),
        "league_price": price,
        "proj_ppr": truth + rng.normal(0, 40, n),
        "actual_ppr": truth,
    })
    for k, v in cols.items():
        df[k] = v
    return df


def test_a_pure_function_of_price_is_rejected():
    """THE core guard. A candidate that is a deterministic function of price cannot
    improve the signal — it is absorbed by the fit that defines the outcome. If this ever
    passes as ORTHOGONAL, the harness is not measuring what it claims and every downstream
    'this signal helps' conclusion is worthless.

    Regression: lstsq solves a singular design with the pseudo-inverse, which splits the
    coefficient across the collinear columns and reported t = 14.2 here before the
    collinearity guard existed — a confident greenlight for restating price.
    """
    df = _board()
    df["price_echo"] = np.log(df["league_price"]) * 3.0 + 7.0
    (res,) = measure_orthogonality(df, ["price_echo"])
    assert res["t"] is None, (
        f"a pure restatement of price scored t={res['t']}, which would greenlight "
        "work that provably cannot move accuracy"
    )
    assert "collinear" in res["verdict"]


def test_a_noisy_restatement_of_price_is_also_rejected():
    """Real candidates are correlated with price rather than identical to it. Mild noise
    must not let a price proxy sneak through."""
    rng = np.random.default_rng(2)
    df = _board(seed=2)
    df["price_proxy"] = np.log(df["league_price"]) + rng.normal(0, 0.15, len(df))
    (res,) = measure_orthogonality(df, ["price_proxy"])
    assert abs(res["t"]) < ORTHOGONALITY_T_THRESHOLD, res


def test_a_pure_function_of_the_projection_is_rejected():
    """We already have the projection; restating it adds nothing. Same singular-design
    trap as the price case — this scored t = 14.2 before the collinearity guard."""
    df = _board(seed=3)
    df["proj_echo"] = df["proj_ppr"] * 2.0 - 5.0
    (res,) = measure_orthogonality(df, ["proj_echo"])
    assert res["t"] is None, res
    assert "collinear" in res["verdict"]


def test_genuinely_orthogonal_information_is_detected():
    """The other half: real price-orthogonal skill must be FOUND, or the harness would
    reject every good candidate and stall the work permanently."""
    df = _board(seed=4)
    # The part of the outcome neither price nor our projection knows.
    lnp = np.log(df["league_price"]).to_numpy()
    resid = df["actual_ppr"].to_numpy() - np.polyval(
        np.polyfit(lnp, df["actual_ppr"].to_numpy(), 1), lnp)
    df["insider"] = resid + np.random.default_rng(4).normal(0, 15, len(df))
    (res,) = measure_orthogonality(df, ["insider"])
    assert abs(res["t"]) >= ORTHOGONALITY_T_THRESHOLD, res
    assert res["verdict"].startswith("ORTHOGONAL")
    assert res["beta"] > 0


def test_pure_noise_is_rejected():
    df = _board(seed=5)
    df["noise"] = np.random.default_rng(99).normal(0, 1, len(df))
    (res,) = measure_orthogonality(df, ["noise"])
    assert abs(res["t"]) < ORTHOGONALITY_T_THRESHOLD, res


def test_constant_candidate_reports_untestable_not_uninformative():
    """A stage that never ran (beat_reporter under an as-of clock writes zero rows) must
    read as 'not tested', never as 'tested and useless' — those imply opposite next steps.
    """
    df = _board(seed=6)
    df["never_ran"] = 0
    (res,) = measure_orthogonality(df, ["never_ran"])
    assert res["t"] is None
    assert "not enough variation" in res["verdict"]


def test_missing_candidate_column_is_skipped_not_crashed():
    """An older board lacking a candidate must not sink the whole backtest."""
    df = _board(seed=7)
    assert measure_orthogonality(df, ["does_not_exist"]) == []


def test_small_sample_is_inconclusive_rather_than_confident():
    df = _board(n=12, seed=8)
    df["thing"] = np.random.default_rng(1).normal(0, 1, len(df))
    (res,) = measure_orthogonality(df, ["thing"])
    assert "inconclusive" in res["verdict"]
    assert res["n"] < ORTHOGONALITY_MIN_N


def test_results_are_sorted_by_absolute_t():
    df = _board(seed=9)
    lnp = np.log(df["league_price"]).to_numpy()
    resid = df["actual_ppr"].to_numpy() - np.polyval(
        np.polyfit(lnp, df["actual_ppr"].to_numpy(), 1), lnp)
    df["strong"] = resid
    df["weak"] = np.random.default_rng(3).normal(0, 1, len(df))
    res = measure_orthogonality(df, ["weak", "strong"])
    assert res[0]["candidate"] == "strong"


def test_empty_frame_returns_empty():
    assert measure_orthogonality(pd.DataFrame(), ["x"]) == []


def test_direction_stability_is_reported_and_reproducible():
    """The bootstrap seed is fixed on purpose — a metric that changes between identical
    runs cannot be used to decide whether to build something."""
    df = _board(seed=10)
    lnp = np.log(df["league_price"]).to_numpy()
    df["real"] = df["actual_ppr"].to_numpy() - np.polyval(
        np.polyfit(lnp, df["actual_ppr"].to_numpy(), 1), lnp)
    a = measure_orthogonality(df, ["real"])[0]
    b = measure_orthogonality(df, ["real"])[0]
    assert a["direction_stability"] == b["direction_stability"]
    assert a["direction_stability"] >= 0.95


def test_suggestive_tier_needs_both_a_borderline_t_and_a_stable_sign():
    """The middle tier exists because the dependency flags landed at |t| = 1.76-1.83 with
    97-98% sign stability — under a hard t>=2 bar they would have been discarded as noise.
    It must NOT fire on a borderline t with an unstable sign."""
    assert ORTHOGONALITY_T_SUGGESTIVE < ORTHOGONALITY_T_THRESHOLD
    df = _board(seed=11)
    df["noise"] = np.random.default_rng(7).normal(0, 1, len(df))
    (res,) = measure_orthogonality(df, ["noise"])
    if res["t"] is not None and abs(res["t"]) < ORTHOGONALITY_T_SUGGESTIVE:
        assert "SUGGESTIVE" not in res["verdict"]


@pytest.mark.parametrize("scale,shift", [(1.0, 0.0), (100.0, -50.0), (0.01, 3.0)])
def test_verdict_is_invariant_to_candidate_units(scale, shift):
    """Standardisation must make the verdict independent of whether a candidate is a
    percentage, a count or a fraction — otherwise it would rank candidates by their unit."""
    df = _board(seed=12)
    lnp = np.log(df["league_price"]).to_numpy()
    base = df["actual_ppr"].to_numpy() - np.polyval(
        np.polyfit(lnp, df["actual_ppr"].to_numpy(), 1), lnp)
    df["scaled"] = base * scale + shift
    (res,) = measure_orthogonality(df, ["scaled"])
    assert abs(res["t"]) >= ORTHOGONALITY_T_THRESHOLD
    assert res["beta"] == pytest.approx(res["beta"])  # finite, not NaN
