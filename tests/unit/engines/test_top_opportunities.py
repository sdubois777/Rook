"""top_opportunities must rank on conviction, not the retired dollar gap.

`value_gap = ai_bid_ceiling - price` is price-contaminated (corr with ln price -0.581).
#378 replaced it with `signal_conviction` (+0.017) as the ranking basis, and the product
sorts on conviction — but the metric kept selecting on the dollar gap, so it reported on
a quantity nothing consumes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from backend.engines.backtest import (
    TOP_OPPORTUNITY_FRACTION,
    TOP_OPPORTUNITY_MIN,
    select_top_opportunities,
)


def _frame(n: int = 40) -> pd.DataFrame:
    """Conviction and the dollar gap rank players in OPPOSITE orders, so the test cannot
    pass by accident on whichever basis is used."""
    rng = np.random.default_rng(4)
    price = np.linspace(1.0, 60.0, n)
    return pd.DataFrame({
        "name": [f"P{i}" for i in range(n)],
        "position": ["RB"] * (n // 2) + ["WR"] * (n - n // 2),
        "league_price": price,
        "value_gap": np.linspace(30.0, -30.0, n),          # highest at the CHEAP end
        "signal_conviction": np.linspace(-2.0, 2.0, n),    # highest at the EXPENSIVE end
        "proj_ppr": 100 + price * 3,
        "actual_ppr": 100 + price * 3 + rng.normal(0, 20, n),
    })


def test_slice_comes_from_the_conviction_end_not_the_dollar_gap_end():
    df = _frame()
    top, basis = select_top_opportunities(df)

    assert basis == "conviction"
    assert top["signal_conviction"].min() > 0, "slice is not from the high-conviction end"
    assert top["league_price"].min() > df["league_price"].median(), (
        "slice came from the cheap/high-dollar-gap end — that is the retired basis"
    )


def test_slice_size_is_the_pre_registered_fraction():
    df = _frame(n=150)
    top, _ = select_top_opportunities(df)
    assert len(top) == int(round(150 * TOP_OPPORTUNITY_FRACTION))


def test_tiny_board_gets_a_floor_not_a_one_player_slice():
    top, _ = select_top_opportunities(_frame(n=9))
    assert len(top) == TOP_OPPORTUNITY_MIN


def test_board_without_conviction_falls_back_and_says_so():
    """A pre-#378 board stores no conviction. It must report the retired basis EXPLICITLY
    rather than silently ranking on something else."""
    df = _frame().drop(columns=["signal_conviction"])
    top, basis = select_top_opportunities(df)

    assert basis == "dollar_gap"
    assert (top["value_gap"] >= 8).all()


def test_unpriced_and_unscored_players_are_excluded():
    df = _frame()
    df.loc[df.index[:10], "league_price"] = 0.0     # undrafted
    df.loc[df.index[10:20], "actual_ppr"] = np.nan  # no result
    top, _ = select_top_opportunities(df)

    assert (top["league_price"] > 0).all()
    assert top["actual_ppr"].notna().all()
