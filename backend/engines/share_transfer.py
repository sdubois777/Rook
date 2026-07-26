"""Share-based sizing for dependency flags — how much a departure or an arrival is worth.

THE DEFECT THIS FIXES. Dependency flags carry a ``value_impact_pct``, and that number is
currently a flat constant (beneficiary 35%, and the model's own guesses elsewhere), so
DK Metcalf's vacated target share and Tyler Lockett's are sized identically. Measured on
the as-of prospective backtest, the consequence is stark:

    flag PRESENCE        |t| = 1.76-1.83, sign stable in 97-98% of bootstraps
    dep_net_impact       t = -0.09        <- the magnitude carries NOTHING

The system knows WHO is affected. Its estimate of HOW MUCH is noise. This module replaces
that guess with a coefficient fitted to what actually happened.

MEASURED, over three offseasons of nflverse target data (2021->22, 22->23, 23->24), with
share defined as a player's season targets over his team's season targets (per-team sums
verified to 1.00 — an earlier attempt used avg_target_share, which is a mean of per-game
shares, sums to ~1.47, and produced a meaningless fit):

  DEPARTURE — incumbents absorb 0.258 of vacated share
      slope +0.258, se 0.089, t +2.91, n = 96 team-seasons.
      Against the flat 0.35 in use today: too high, and structurally unable to scale
      with how much actually departed.

  ARRIVAL — incumbents lose share IN PROPORTION to what they already had
      proportional term (before x arrival)  -0.622, t -10.84
      flat term                             -0.006, t  -0.67   <- nothing
      n = 919 incumbent player-seasons.
      So dilution is proportional, not flat: a single multiplier applies to every
      incumbent, and the WR1 loses most in absolute terms. Per position:
      WR -0.655, TE -0.497, RB -0.757.

BOTH DIRECTIONS MATTER. A departing WR1 lifts the room; an arriving one dilutes it. The
negative case is the same arithmetic with the sign flipped, and it is what should be
sizing displaced/committee flags.

WHAT THIS CANNOT DO. The market already prices the obvious part — "Metcalf left, Seattle's
receivers get more" is in ADP. The edge here is precision, not direction, so expect a
modest gain. Run any change through ``measure_orthogonality`` before believing it helps.

Pure functions, no I/O, no API calls.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Fraction of a departing player's share that returning team-mates actually absorb.
# The rest goes to new signings, rookies, and scheme change — it does NOT stay in the room.
DEPARTURE_ABSORPTION = 0.258

# Fraction of an arriving player's share taken FROM incumbents, proportional to what each
# already held. Position-specific; the pooled figure is 0.622.
ARRIVAL_DILUTION = {"WR": 0.655, "TE": 0.497, "RB": 0.757}
ARRIVAL_DILUTION_DEFAULT = 0.622

# value_impact_pct is Numeric(4,2) — clamp so a bad share never overflows the column or
# emits an implausible claim like "-140% of his value".
_MAX_ABS_IMPACT_PCT = 60.0

# UNITS: everything here is a PERCENT (12.0 means 12%), matching the column name and the
# model-generated flags. The deterministic paths in roster_changes previously wrote
# FRACTIONS (0.35) into the same column, so ~60% of stored rows were ~100x smaller than
# the rest — measured on the live board: contingent ranged 0.12 to 70.00 with 140 rows
# under |1| and 99 over. That mixture is the likeliest reason dep_net_impact measured
# t = -0.09 (pure noise) while flag PRESENCE was significant.

# Fallbacks for when share data is unavailable. Derived from the SAME fits, evaluated at
# the median observed shares, so they sit on the same scale as the computed values rather
# than being invented:
#   beneficiary: 0.258 * 0.246 (median vacated) / 0.50 (typical incumbent room) ~= 12.7%
#   displaced:   0.622 * 0.246 ~= 15.3%
# Used so a flag keeps its PRESENCE (which is the part that measured as informative)
# instead of being dropped when shares cannot be computed.
DEFAULT_BENEFICIARY_PCT = 12.7
DEFAULT_DISPLACED_PCT = -15.3


def _clamp(pct: float) -> float:
    return round(max(-_MAX_ABS_IMPACT_PCT, min(_MAX_ABS_IMPACT_PCT, pct)), 2)


def beneficiary_impact_pct(
    vacated_share: float,
    incumbent_total_share: float,
) -> float | None:
    """Percent uplift for an incumbent when team-mates totalling ``vacated_share`` leave.

    The absorbed share is split across incumbents in proportion to what each already had,
    so every incumbent's RELATIVE uplift is the same::

        absorbed        = DEPARTURE_ABSORPTION * vacated_share
        uplift_i        = absorbed * (share_i / incumbent_total_share)
        relative_i      = uplift_i / share_i = absorbed / incumbent_total_share

    which is why this takes the team total rather than the individual's share. Returns
    None when the inputs cannot support a claim, so the caller can leave the model's own
    estimate alone rather than writing a fabricated zero.
    """
    if vacated_share is None or incumbent_total_share is None:
        return None
    v, t = float(vacated_share), float(incumbent_total_share)
    if v <= 0 or t <= 0:
        return None
    return _clamp(100.0 * DEPARTURE_ABSORPTION * v / t)


def displaced_impact_pct(
    arrival_share: float,
    position: str | None = None,
) -> float | None:
    """Percent hit to an incumbent when arrivals totalling ``arrival_share`` join.

    Dilution is proportional to what the incumbent already had, and the flat component
    measured as nothing (t = -0.67), so the RELATIVE hit is the same for everyone in the
    room and does not depend on the individual's share::

        relative_loss_i = ARRIVAL_DILUTION[pos] * arrival_share

    Negative by construction. Returns None when there is no arrival to price.
    """
    if arrival_share is None:
        return None
    a = float(arrival_share)
    if a <= 0:
        return None
    coef = ARRIVAL_DILUTION.get((position or "").upper(), ARRIVAL_DILUTION_DEFAULT)
    return _clamp(-100.0 * coef * a)


def impact_for_flag(
    flag_type: str,
    *,
    vacated_share: float | None = None,
    incumbent_total_share: float | None = None,
    arrival_share: float | None = None,
    position: str | None = None,
) -> float | None:
    """Size any dependency flag from share data. None => keep the existing estimate.

    ``beneficiary`` and ``contingent`` price a departure; ``displaced`` and ``committee``
    price an arrival. Anything else has no share-based interpretation and is left alone —
    ``scheme_fit`` and ``college_trust`` are judgements about a player, not about how a
    target room was redistributed, and inventing a share for them would be fabrication.
    """
    ft = (flag_type or "").strip().lower()
    if ft in ("beneficiary", "contingent"):
        return beneficiary_impact_pct(vacated_share, incumbent_total_share)
    if ft in ("displaced", "committee"):
        return displaced_impact_pct(arrival_share, position)
    return None
