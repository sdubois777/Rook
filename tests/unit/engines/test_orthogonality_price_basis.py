"""The orthogonality harness must not credit a transform of the PRICE as new information.

`measure_orthogonality` decides which candidate signals are worth building on, so a
false positive there propagates into every downstream signal decision. It controlled for
z(ln price) LINEARLY only, which let any nonlinear function of the price through: not
collinear with the linear term, so it cleared the collinearity guard, and the fit then
credited it with variance the price already explained.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.engines.backtest import CANDIDATE_SIGNALS, measure_orthogonality


def _board(seed: int = 0, n: int = 160) -> pd.DataFrame:
    """A board where the outcome is driven by price and noise ONLY.

    Nothing here carries information beyond the price, so an honest harness must find
    no orthogonal signal in any function of the price.
    """
    rng = np.random.default_rng(seed)
    pos = rng.choice(["QB", "RB", "WR", "TE"], n, p=[0.15, 0.3, 0.4, 0.15])
    price = np.round(np.exp(rng.uniform(0.0, 4.2, n))).clip(1, 70)
    lnp = np.log(price)
    # outcome is a NONLINEAR function of price plus noise
    actual = 40 + 55 * lnp - 4.0 * lnp ** 2 + rng.normal(0, 28, n)
    proj = 40 + 52 * lnp + rng.normal(0, 34, n)
    return pd.DataFrame({
        "position": pos, "league_price": price.astype(float),
        "actual_ppr": actual, "proj_ppr": proj,
    })


@pytest.mark.parametrize("null_name,build", [
    ("null_lnp_squared", lambda d: np.log(d["league_price"]) ** 2),
    ("null_price_rank_cubic",
     lambda d: d.groupby("position")["league_price"].rank(ascending=False) ** 3),
    ("null_sqrt_price", lambda d: np.sqrt(d["league_price"])),
])
def test_a_pure_function_of_price_is_never_orthogonal(null_name, build):
    """REGRESSION: ln(price)**2 previously scored t=1.80 / stability 0.963 on the real
    2024 board and was reported as SUGGESTIVE. It knows nothing the price does not."""
    d = _board()
    d[null_name] = build(d)

    result = measure_orthogonality(d, [null_name])
    assert result, "candidate was not evaluated at all"
    verdict = result[0]["verdict"]

    assert "ORTHOGONAL" not in verdict, (
        f"{null_name} is a function of price alone but was reported as: {verdict}"
    )
    assert "SUGGESTIVE" not in verdict, (
        f"{null_name} is a function of price alone but was reported as: {verdict}"
    )


def test_genuinely_independent_information_still_registers():
    """The guard must not be so aggressive that it rejects a real orthogonal signal."""
    d = _board(seed=3)
    rng = np.random.default_rng(9)
    real = rng.normal(0, 1, len(d))
    d["real_signal"] = real
    # give it a genuine effect on the outcome that the price does not contain
    d["actual_ppr"] = d["actual_ppr"] + 34.0 * real

    result = measure_orthogonality(d, ["real_signal"])
    assert result[0]["t"] is not None
    assert abs(result[0]["t"]) >= 2.0, (
        f"a real orthogonal signal was suppressed: t={result[0]['t']}"
    )
    assert "ORTHOGONAL" in result[0]["verdict"]


def test_candidate_coefficient_is_reported_not_a_control():
    """The reported beta/t must belong to the CANDIDATE, not to a price control.

    The candidate used to be hardcoded at index 2. Widening the price basis without
    moving that index would silently report a price term's coefficient instead.
    """
    d = _board(seed=5)
    rng = np.random.default_rng(11)
    d["planted"] = rng.normal(0, 1, len(d))
    d["actual_ppr"] = d["actual_ppr"] + 60.0 * d["planted"]

    res = measure_orthogonality(d, ["planted"])[0]
    assert res["beta"] is not None and res["beta"] > 0.25, (
        f"expected the planted positive coefficient, got beta={res['beta']} "
        "(a price control's coefficient would not track the plant)"
    )


def test_dep_contingent_is_not_a_separate_candidate():
    """It is the same column as dep_displaced (corr 1.0000 / 0.9665 on the real boards).
    Testing both reported one signal twice as if two candidates agreed."""
    assert "dep_displaced" in CANDIDATE_SIGNALS
    assert "dep_contingent" not in CANDIDATE_SIGNALS
