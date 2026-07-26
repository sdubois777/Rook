"""The projection-relative signal basis.

These are accuracy guards, not just unit tests. The whole point of this module is that
the shipped basis (dollar gap ``ai_bid_ceiling - market``) measured WORSE than the
projection underneath it on the as-of 2025 prospective backtest. If a change here
silently reverts to a price-biased or dollar-magnitude basis, the board goes back to
ranking its best opportunities at 46.7% — worse than a coin flip — and nothing else in
the suite would notice.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from backend.engines.signal_basis import (
    CONVICTION_STRONG,
    CONVICTION_WEAK,
    MIN_DISTINCT_PRICES,
    MIN_PLAYERS_FOR_CURVE,
    PriceCurve,
    conviction_to_gap_signal,
    derive_signals_from_conviction,
    fit_price_curve,
)

# Preserved per-player rows from the as-of 2025 prospective backtest. The board itself no
# longer exists (the schema has no season dimension and 2026 was restored over it), so
# this file is the only surviving record and the only way to regression-test accuracy.
ROWS = Path(__file__).resolve().parents[3] / "docs" / "recon" / "asof_2025_backtest_rows.csv"


def _load():
    """Priced, scoreable players with a projection: the population the signal acts on."""
    if not ROWS.exists():
        pytest.skip(f"backtest rows not present at {ROWS}")
    out = []
    with ROWS.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                price = float(r["league_price"] or 0)
                proj = float(r["proj_ppr"] or 0)
                actual = float(r["actual_ppr"] or 0)
                implied = float(r["price_implied_ppr"] or 0)
                ceiling = float(r["ai_ceiling"] or 0)
            except ValueError:
                continue
            if price <= 0 or proj <= 0 or not r["actual_ppr"] or not r["price_implied_ppr"]:
                continue
            out.append({
                "name": r["name"], "position": r["position"], "price": price,
                "proj": proj, "actual": actual, "implied": implied, "ceiling": ceiling,
                # The scored outcome: did he beat what his own price predicted?
                "good": actual > implied,
            })
    return out


def _curves(rows):
    by_pos: dict[str, list] = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r)
    return {
        pos: fit_price_curve([x["proj"] for x in rs], [x["price"] for x in rs])
        for pos, rs in by_pos.items()
    }


# ---------------------------------------------------------------------------
# The accuracy guards
# ---------------------------------------------------------------------------

def test_new_basis_beats_the_dollar_gap_on_real_backtest_data():
    """THE regression guard: conviction must out-score ``ceiling - market``.

    Measured when this shipped: 60.3% vs 55.6% over 151 priced players, with the ceiling
    used directly at 51.7% (paired exact McNemar p = 0.029 against the projection). The
    bar below is deliberately loose — it asserts the ORDERING holds, not the exact
    figure, because a single season carries a ±8 point bootstrap CI.
    """
    rows = _load()
    curves = _curves(rows)
    new_hits = old_hits = n = 0
    for r in rows:
        c = curves.get(r["position"])
        if c is None:
            continue
        z = c.conviction(r["proj"], r["price"])
        if z is None:
            continue
        n += 1
        new_hits += (z > 0) == r["good"]
        old_hits += ((r["ceiling"] - r["price"]) > 0) == r["good"]
    assert n >= 100, f"expected the full priced board, got {n}"
    new_acc, old_acc = new_hits / n, old_hits / n
    assert new_acc > old_acc, (
        f"the new basis ({new_acc:.1%}) no longer beats the dollar gap ({old_acc:.1%}) — "
        "this is the entire reason signal_basis exists"
    )
    assert new_acc >= 0.57, f"accuracy regressed to {new_acc:.1%} (was 60.3% when shipped)"


def test_conviction_is_price_neutral():
    """The defect being fixed: the dollar gap correlates with price, so ranking by it
    sorts by expensiveness. Conviction must not. Measured +0.017 vs −0.581."""
    rows = _load()
    curves = _curves(rows)
    zs, gaps, lnp = [], [], []
    for r in rows:
        c = curves.get(r["position"])
        if c is None:
            continue
        z = c.conviction(r["proj"], r["price"])
        if z is None:
            continue
        zs.append(z)
        gaps.append(r["ceiling"] - r["price"])
        lnp.append(math.log(r["price"]))

    def corr(a, b):
        n = len(a)
        ma, mb = sum(a) / n, sum(b) / n
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        va = math.sqrt(sum((x - ma) ** 2 for x in a))
        vb = math.sqrt(sum((y - mb) ** 2 for y in b))
        return cov / (va * vb)

    z_corr, gap_corr = corr(zs, lnp), corr(gaps, lnp)
    assert abs(z_corr) < 0.20, f"conviction became price-biased (corr={z_corr:+.3f})"
    assert abs(z_corr) < abs(gap_corr), (
        f"conviction (corr={z_corr:+.3f}) is no better than the dollar gap "
        f"({gap_corr:+.3f}) at being price-neutral"
    )


def test_high_conviction_calls_are_better_than_the_whole_board():
    """Lever 2: confidence must mean something. The OLD basis got WORSE the more
    confident it was (top 20% of the board by dollar gap scored 46.7%), which is why
    top_opportunities landed at exactly 50.0% in the backtest."""
    rows = _load()
    curves = _curves(rows)
    scored = []
    for r in rows:
        c = curves.get(r["position"])
        if c is None:
            continue
        z = c.conviction(r["proj"], r["price"])
        if z is not None:
            scored.append((abs(z), (z > 0) == r["good"]))
    scored.sort(key=lambda t: -t[0])
    top = scored[: max(8, len(scored) // 5)]
    all_acc = sum(h for _, h in scored) / len(scored)
    top_acc = sum(h for _, h in top) / len(top)
    assert top_acc >= all_acc, (
        f"high-conviction calls ({top_acc:.1%}) are no better than the whole board "
        f"({all_acc:.1%}) — conviction carries no information"
    )


def test_dollar_edge_sign_always_matches_conviction():
    """The displayed dollar figure must never contradict the assessment chip."""
    rows = _load()
    curves = _curves(rows)
    checked = 0
    for r in rows:
        c = curves.get(r["position"])
        if c is None:
            continue
        z = c.conviction(r["proj"], r["price"])
        d = c.dollar_edge(r["proj"], r["price"])
        if z is None or d is None:
            continue
        checked += 1
        if abs(z) > 1e-9:
            assert (z > 0) == (d > 0), (
                f"{r['name']}: conviction {z:+.2f} but dollar edge {d:+.1f} — a user "
                "would see a positive gap under an 'avoid' label"
            )
    assert checked >= 100


# ---------------------------------------------------------------------------
# Fit behaviour — the fallback path is what protects the pipeline
# ---------------------------------------------------------------------------

def test_fit_refuses_small_samples_rather_than_inventing_a_curve():
    """Too few players → None → caller falls back to the legacy basis. Fitting a line
    through four points and shipping it as "the market's price curve" is how a position
    silently gets garbage signals."""
    pts = [100.0, 150.0, 200.0]
    prices = [1.0, 5.0, 20.0]
    assert fit_price_curve(pts, prices) is None
    assert len(pts) < MIN_PLAYERS_FOR_CURVE


def test_fit_refuses_when_prices_are_degenerate():
    """All-equal prices carry no slope information."""
    n = MIN_PLAYERS_FOR_CURVE + 4
    assert fit_price_curve([100.0 + i for i in range(n)], [5.0] * n) is None
    assert MIN_DISTINCT_PRICES >= 2


def test_fit_refuses_a_negative_slope():
    """A market that pays LESS for more production is a broken column or a degenerate
    sample. Using its inverse would flip every call on that position, silently."""
    n = MIN_PLAYERS_FOR_CURVE + 4
    prices = [float(i + 1) for i in range(n)]
    points = [500.0 - 40.0 * i for i in range(n)]      # strictly decreasing in price
    assert fit_price_curve(points, prices) is None


def test_fit_recovers_a_known_curve():
    """Sanity: near-exact log-linear data recovers its own coefficients."""
    prices = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 3.0, 6.0, 12.0, 24.0]
    a, b = 50.0, 30.0
    # A whisper of noise: a PERFECT fit has zero residual spread, which is the degenerate
    # case covered separately below.
    points = [a + b * math.log(p) + (0.01 if i % 2 else -0.01) for i, p in enumerate(prices)]
    c = fit_price_curve(points, prices)
    assert c is not None
    assert c.intercept == pytest.approx(a, abs=0.5)
    assert c.slope == pytest.approx(b, abs=0.5)


def test_perfectly_fitting_data_yields_no_curve():
    """Zero residual spread means nobody is above or below the market curve, so
    conviction is 0/0. Return None so the caller falls back rather than dividing by zero.
    Cannot happen with real data; asserted so the guard is not "cleaned up"."""
    prices = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 3.0, 6.0, 12.0, 24.0]
    points = [50.0 + 30.0 * math.log(p) for p in prices]
    assert fit_price_curve(points, prices) is None


def test_conviction_is_positive_above_the_curve_and_negative_below():
    prices = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 3.0, 6.0, 12.0, 24.0]
    points = [50.0 + 30.0 * math.log(p) for p in prices]
    points[0] += 40.0      # over-performer at $1
    points[-1] -= 40.0     # under-performer at $24
    c = fit_price_curve(points, prices)
    assert c is not None
    assert c.conviction(points[0], prices[0]) > 0
    assert c.conviction(points[-1], prices[-1]) < 0


def test_no_price_yields_no_opinion():
    prices = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 3.0, 6.0, 12.0, 24.0]
    points = [50.0 + 30.0 * math.log(p) + (i % 3) for i, p in enumerate(prices)]
    c = fit_price_curve(points, prices)
    assert c is not None
    assert c.conviction(120.0, 0) is None
    assert c.dollar_edge(120.0, 0) is None
    assert c.residual(120.0, None) is None


def test_implied_price_is_clamped_both_ends():
    """The inverse is exponential — unclamped it produces four-figure 'values'."""
    c = PriceCurve(intercept=50.0, slope=5.0, resid_sd=10.0, n=20, price_cap=90.0)
    assert c.implied_price(10_000.0) == 90.0     # capped
    assert c.implied_price(-10_000.0) == 1.0     # floored, never zero or negative


# ---------------------------------------------------------------------------
# Band derivation — same vocabulary as the legacy path, so consumers are unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("z,assessment,pay_up,nom", [
    (3.0, "elite_value", True, False),
    (1.25, "elite_value", True, False),       # boundary: >= strong
    (1.24, "good_value", False, False),
    (0.51, "good_value", False, False),
    (0.50, "fair_value", False, False),       # boundary: weak band is inclusive
    (0.0, "fair_value", False, False),
    (-0.50, "fair_value", False, False),
    (-0.51, "slight_overpay", False, False),
    (-1.25, "slight_overpay", False, False),  # boundary: >= -strong
    (-1.26, "avoid", False, True),
    (-4.0, "avoid", False, True),
])
def test_conviction_bands(z, assessment, pay_up, nom):
    assert derive_signals_from_conviction(z) == (assessment, pay_up, nom)


def test_no_conviction_makes_no_claim():
    """Absent a basis we emit no market-relative label, rather than a neutral guess that
    would read as 'we looked and he is fairly priced'."""
    assert derive_signals_from_conviction(None) == (None, False, False)
    assert conviction_to_gap_signal(None) is None


def test_bands_are_monotone_and_flags_are_exclusive():
    order = ["elite_value", "good_value", "fair_value", "slight_overpay", "avoid"]
    prev = -1
    z = 3.0
    while z >= -3.0:
        a, pu, nm = derive_signals_from_conviction(z)
        assert not (pu and nm), f"both flags fired at z={z}"
        idx = order.index(a)
        assert idx >= prev, f"assessment improved as conviction fell, at z={z}"
        prev = idx
        z -= 0.25


def test_gap_signal_agrees_with_assessment_direction():
    """value_gap_signal and value_assessment are rendered together in one chip; they must
    never point opposite ways."""
    z = 3.0
    while z >= -3.0:
        assessment, _, _ = derive_signals_from_conviction(z)
        sig = conviction_to_gap_signal(z)
        if assessment in ("elite_value", "good_value"):
            assert sig == "market_undervalues", f"z={z}: {assessment} but {sig}"
        elif assessment in ("avoid", "slight_overpay"):
            assert sig == "market_overvalues", f"z={z}: {assessment} but {sig}"
        else:
            assert sig == "aligned", f"z={z}: {assessment} but {sig}"
        z -= 0.25


def test_thresholds_are_ordered():
    assert 0 < CONVICTION_WEAK < CONVICTION_STRONG
