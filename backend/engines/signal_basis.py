"""Projection-relative signal basis — what buy/avoid is derived FROM.

WHY THIS EXISTS. Signals used to come from the dollar gap
``ai_bid_ceiling - market_value_fantasypros``. Measured on the as-of 2025 prospective
backtest (151 priced players, real auction prices), that basis is materially worse than
the projection it is built from:

    raw projection, residual vs price      60.3%
    ai_bid_ceiling, residual vs price      51.7%     <- paired McNemar p = 0.029
    raw dollar gap (ceiling - market)      55.6%
    the shipped system_signal              56.9%

``ai_bid_ceiling`` also adds only +0.0005 R^2 beyond the projection — i.e. nothing. Two
measured causes: the ceiling correlates 0.759 with the very price it is meant to beat,
and it is an INTEGER dollar value that saturates at the bottom of the board (in the
$0-5 band, 24 distinct projections collapse into 5 distinct ceilings). Rank correlation
survives (spearman 0.96-0.98 at RB/WR/TE), so this is not a reordering — it is a
compressive, market-contaminated rounding that destroys the fine ordering.

WHAT REPLACES IT. Within each position, fit what the market actually charges for
production:

    projected_points = a + b * ln(price)

and take the residual. A player is a BUY when he projects above what his own price
implies. Least squares centres the residuals at zero, so this is price-neutral by
construction — measured ``corr(conviction, ln price) = +0.017`` against ``+0.684`` for
the old dollar basis.

WHY THE CONVICTION IS STANDARDISED, NOT DOLLARS. The residual can be re-expressed in
dollars by inverting the fit, and the SIGN is identical (so accuracy is identical). But
the inverse is exponential, so the dollar magnitude explodes for expensive players and
ranking by it reintroduces exactly the price-band artifact it was meant to remove:

    conviction ranking, top 20% of board     by dollar gap  46.7%
                                             by |z|         66.7%

That is why ``top_opportunities`` scored exactly 50.0% (14/28) in the backtest — the
board was ranked by the wrong quantity. Conviction here is therefore the residual
standardised WITHIN POSITION.

WHAT THIS DELIBERATELY DOES NOT TOUCH. ``ai_bid_ceiling`` remains the auction bid
surface and ``value_gap`` remains ``ceiling - market`` — bidding, budget maths and the
live-draft engine are unaffected. Only the JUDGEMENT fields (value_assessment,
pay_up_flag, nomination_target_flag, value_gap_signal) move onto this basis.

NOT A SPREAD FIX. Rescaling projection spread within a position is a NO-OP here: a
linear rescale multiplies every residual by the same factor, leaving sign and z
unchanged. Measured — signal accuracy is 60.3% at every multiplier from 0.6x to 1.77x.
Spread only moves MAE, and the projection is already OVER-dispersed (calibration slope
of actual on projected = 0.688 < 1), so expanding it makes MAE worse. Do not add a
spread knob here expecting it to move signal.

Pure functions, no I/O, no API calls — this module is deterministic and free to run.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# A position needs enough priced players, and enough distinct prices, before a
# points-vs-price line means anything. Below this the caller falls back to the legacy
# dollar-gap basis rather than inventing a curve from four points.
MIN_PLAYERS_FOR_CURVE = 8
MIN_DISTINCT_PRICES = 3

# Ceiling on the exponential inverse used for the DISPLAY dollar figure, as a multiple of
# the priciest player actually seen at that position.
PRICE_CAP_MULTIPLE = 1.5

# Residual spread below this fraction of the points scale is treated as no spread at all.
_DEGENERATE_SD_FRACTION = 1e-6

# Assessment cuts on the standardised residual. Chosen ROUND and to keep the board's
# bucket distribution close to what shipped (elite 13 vs 9, avoid 13 vs 14, on the 2025
# board) — deliberately NOT tuned to maximise accuracy on a single season, which carries
# a +/-8 point bootstrap CI and would be textbook overfitting.
CONVICTION_STRONG = 1.25
CONVICTION_WEAK = 0.50


@dataclass(frozen=True)
class PriceCurve:
    """What the market charges for production at one position: points = a + b*ln(price)."""
    intercept: float
    slope: float
    resid_sd: float
    n: int
    price_cap: float

    def residual(self, points: float, price: float) -> Optional[float]:
        if price is None or price <= 0:
            return None
        return float(points) - (self.intercept + self.slope * math.log(float(price)))

    def conviction(self, points: float, price: float) -> Optional[float]:
        """Residual standardised within position. Positive = projects above his price."""
        r = self.residual(points, price)
        if r is None or self.resid_sd <= 0:
            return None
        return r / self.resid_sd

    def implied_price(self, points: float) -> float:
        """The price the market's own curve would put on this much production.

        Inverse of the fit. Clamped to ``[1, price_cap]`` because the inverse is
        exponential: an elite projection at a position with a shallow slope extrapolates
        to a dollar value far outside any real auction. The clamp never crosses zero, so
        it cannot flip the sign of the resulting edge.

        The clamp is applied to the EXPONENT, not to its result: ``math.exp`` raises
        OverflowError above ~709, so clamping afterwards would crash on exactly the
        extreme inputs the clamp exists to tame.
        """
        exponent = (float(points) - self.intercept) / self.slope
        lo, hi = 0.0, math.log(self.price_cap)      # ln(1) = 0
        return float(math.exp(min(max(exponent, lo), hi)))

    def dollar_edge(self, points: float, price: float) -> Optional[float]:
        """implied_price - price, for DISPLAY.

        Sign always matches :meth:`conviction` (both are monotone in the same residual,
        and the clamp cannot cross the price), so the number a user sees never
        contradicts the assessment chip. Do NOT rank on this — see the module docstring;
        the dollar magnitude is price-biased and ranking by it scored 46.7% in the top
        20% of the board. Rank on ``conviction``.
        """
        if price is None or price <= 0:
            return None
        return self.implied_price(points) - float(price)


def fit_price_curve(
    points: Sequence[float], prices: Sequence[float],
) -> Optional[PriceCurve]:
    """Least-squares fit of projected points on ln(price) for ONE position.

    Returns None — meaning "no opinion, fall back to the legacy basis" — when there are
    too few players, too few distinct prices, or the fitted slope is non-positive. A
    non-positive slope would mean the market pays LESS for more production at this
    position, which is either a broken market column or a degenerate sample; either way
    the inverse is meaningless and silently using it would flip every call.
    """
    pairs = [
        (float(p), float(pr)) for p, pr in zip(points, prices)
        if p is not None and pr is not None and float(pr) > 0 and float(p) > 0
    ]
    if len(pairs) < MIN_PLAYERS_FOR_CURVE:
        return None
    if len({round(pr, 4) for _, pr in pairs}) < MIN_DISTINCT_PRICES:
        return None

    xs = [math.log(pr) for _, pr in pairs]
    ys = [p for p, _ in pairs]
    n = len(pairs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    if slope <= 0:
        logger.warning(
            "signal_basis: non-positive points-vs-price slope (%.3f) over %d players — "
            "falling back to the legacy dollar-gap basis for this position", slope, n,
        )
        return None
    intercept = my - slope * mx

    resids = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    # Population sd: residuals are already centred by the fit, and we want the spread of
    # THIS board, not an inference about a wider one.
    var = sum(r * r for r in resids) / n
    sd = math.sqrt(var)
    # Scale-relative, not `sd <= 0`. An exact fit leaves sd at floating-point residue
    # (~1e-14, not 0), and dividing a residual by that yields conviction in the millions —
    # every player would land in elite_value or avoid. Degenerate either way: fall back.
    if sd <= _DEGENERATE_SD_FRACTION * max(1.0, abs(my)):
        return None
    # Bound the exponential inverse at 1.5x the priciest player actually seen at this
    # position. Beyond that a "value" is an extrapolation artifact, not an auction number.
    cap = max(pr for _, pr in pairs) * PRICE_CAP_MULTIPLE
    return PriceCurve(
        intercept=intercept, slope=slope, resid_sd=sd, n=n, price_cap=cap,
    )


def derive_signals_from_conviction(
    conviction: Optional[float],
) -> tuple[Optional[str], bool, bool]:
    """(value_assessment, pay_up_flag, nomination_target_flag) from standardised conviction.

    Same vocabulary and same monotonic shape as the legacy dollar-gap derivation, so every
    downstream consumer (backtest.derive_system_signal, the client chip, snake flags) is
    unchanged — only the quantity underneath is better.

        z >= +1.25  → elite_value,    pay_up=True
        z >  +0.50  → good_value
        |z| <= 0.50 → fair_value
        z >= -1.25  → slight_overpay
        z <  -1.25  → avoid,          nomination_target=True

    None conviction → no market-relative claim at all, rather than a neutral guess.
    """
    if conviction is None:
        return None, False, False
    z = float(conviction)
    if z >= CONVICTION_STRONG:
        return "elite_value", True, False
    if z > CONVICTION_WEAK:
        return "good_value", False, False
    if z >= -CONVICTION_WEAK:
        return "fair_value", False, False
    if z >= -CONVICTION_STRONG:
        return "slight_overpay", False, False
    return "avoid", False, True


def conviction_to_gap_signal(conviction: Optional[float]) -> Optional[str]:
    """value_gap_signal on the same basis, so the chip cannot contradict the assessment."""
    if conviction is None:
        return None
    z = float(conviction)
    if z > CONVICTION_WEAK:
        return "market_undervalues"
    if z < -CONVICTION_WEAK:
        return "market_overvalues"
    return "aligned"
