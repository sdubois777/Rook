"""
Pre-draft availability discount — a DETERMINISTIC, cause-aware games-missed
proration on the pre-draft projection/value.

The existing valuation discounts injury-PRONENESS (history/durability). It does
NOT discount a player for a KNOWN CURRENT ABSENCE — a healthy-history WR on PUP
returning Week 8 gets full value. THIS module closes that: given a structured
unavailability designation (PUP / long-term IR / suspension), it computes an
availability factor in (0, 1] that prorates the draft-ranked value. Pure/
synchronous arithmetic — NO Sonnet (auditable, non-metered, re-runnable).

WEIGHTED (less-than-linear) proration — the founder decision (the Rashee Rice
case: suspended 6 weeks, went ~$8, then 20+ ppg). Straight proration
(games_played / 17) UNDERRATES a stud who misses early weeks, because the weeks he
DOES play retain full per-week value and a stud's weeks are scarce. So the discount
is CONVEX — missing 6/17 discounts LESS than the raw 6/17 fraction:

    discount = (games_missed / SEASON_GAMES) ** _PRORATION_EXPONENT   (exponent > 1)
    factor   = 1 - discount

(Note: the task brief wrote "p<1", but the stated INTENT — retain MORE value than
straight for a partial-season stud — requires exponent > 1, since x**p < x for
0<x<1 only when p>1. We implement the intent.) At exponent 1.5: 6 missed →
discount 0.21 (factor 0.79, well above straight's 0.65); 12 missed → discount 0.59
(factor 0.41, still heavily discounted).

CAUSE-AWARE (suspension ≠ injury):
  * SUSPENSION / HOLDOUT — clean return at full strength → prorate the missed weeks
    ONLY (no return haircut).
  * INJURY / IR / PUP — may ramp / carry re-injury risk → prorate missed weeks AND a
    SMALL extra return haircut. Kept modest + tunable and DELIBERATELY small so it
    does not double-count the existing injury-proneness risk term (which already
    prices durability/re-injury history). This term is only the CURRENT injury's
    ramp, not history.

Q / D (day-to-day) are NOT discounted — they play (consistent with start/sit +
waiver treating Q as flagged-not-benched). Only multi-week STRUCTURED absences
prorate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# Fantasy season basis: 17 regular-season games (18 weeks incl. 1 bye).
SEASON_GAMES = 17

# Convex proration exponent (> 1 → played weeks retain value; the Rice fix). ONE knob.
_PRORATION_EXPONENT = 1.5

# Per-designation DEFAULT expected games missed — NO source carries a real weeks_out
# (Sleeper injury_start_date is unpopulated; import_injuries has no duration), so these
# are honest status-tier defaults. An explicit ``weeks_out`` (future news extraction)
# overrides them. Tunable in ONE place.
_DEFAULT_GAMES_MISSED = {
    "pup": 6,             # PUP-R: min 4 missed, returns wks 5-8 → ~6
    "ir_long": 13,        # long-term / likely season-ending offseason IR (heavy, est.)
    "suspension": 4,      # a typical suspension when length is unknown
}

# INJURY return haircut (ramp + re-injury for the CURRENT injury) — small, so it does
# NOT double-count the existing injury-proneness risk term. Applied to the injury/PUP
# post-proration factor only; suspension (clean return) gets NONE.
_INJURY_RETURN_HAIRCUT = 0.05


class Cause(str, Enum):
    SUSPENSION = "suspension"   # clean return at full strength
    INJURY = "injury"           # injury / IR / PUP — ramp + re-injury risk


@dataclass(frozen=True)
class AvailabilityResult:
    games_missed: int
    factor: float          # in (0, 1]; 1.0 = fully available (no discount)
    cause: Optional[Cause]
    reason: str            # human/audit string ("" when no discount)


# Designations we prorate → (default games-missed key, cause).
_DESIGNATION_SPEC: dict[str, tuple[str, Cause]] = {
    "pup": ("pup", Cause.INJURY),
    "ir_long": ("ir_long", Cause.INJURY),
    "suspension": ("suspension", Cause.SUSPENSION),
}

# Structured statuses that DON'T prorate (day-to-day play, or healthy).
_NO_DISCOUNT = frozenset({"q", "d", "o", "active", "healthy", "", "none"})


def _none_result() -> AvailabilityResult:
    return AvailabilityResult(games_missed=0, factor=1.0, cause=None, reason="")


def compute_availability(
    designation: Optional[str],
    *,
    weeks_out: Optional[int] = None,
) -> AvailabilityResult:
    """Compute the availability factor for one structured designation.

    ``designation`` is a canonical key: "pup" | "ir_long" | "suspension" (multi-week
    structured absences) or a no-discount marker (q/d/o/active/None). ``weeks_out``,
    when known (future news extraction), overrides the per-designation default —
    otherwise a documented status-tier default is used. Any UNMAPPED designation
    loud-warns and returns NO discount (never silently discards a player)."""
    key = (designation or "").strip().lower()
    if key in _NO_DISCOUNT:
        return _none_result()
    spec = _DESIGNATION_SPEC.get(key)
    if spec is None:
        logger.warning(
            "availability: unmapped designation %r — NO discount applied "
            "(add it to backend/engines/availability.py if it should prorate)",
            designation,
        )
        return _none_result()

    default_key, cause = spec
    games_missed = int(weeks_out) if weeks_out is not None else _DEFAULT_GAMES_MISSED[default_key]
    games_missed = max(0, min(SEASON_GAMES, games_missed))
    if games_missed == 0:
        return _none_result()

    # Weighted convex proration — played weeks keep full per-week value (Rice fix).
    discount = (games_missed / SEASON_GAMES) ** _PRORATION_EXPONENT
    factor = 1.0 - discount

    # Cause modifier: injury/IR/PUP take a small extra return haircut (ramp / re-injury
    # of the CURRENT injury); suspension returns clean → none. Kept small to avoid
    # double-counting the existing injury-proneness risk term.
    if cause is Cause.INJURY:
        factor *= (1.0 - _INJURY_RETURN_HAIRCUT)

    factor = max(0.0, min(1.0, round(factor, 4)))
    src = "weeks_out" if weeks_out is not None else "default"
    reason = (
        f"{key}: ~{games_missed}/{SEASON_GAMES} games missed ({src}), "
        f"{cause.value} → availability {factor:.2f}"
    )
    return AvailabilityResult(games_missed=games_missed, factor=factor, cause=cause, reason=reason)


def designation_from_sleeper(status: Optional[str], injury_status: Optional[str]) -> Optional[str]:
    """Map Sleeper's structured ``status`` (+ canonical ``injury_status``) to a
    canonical availability designation, or None (no structured multi-week absence).

    Sleeper ``status`` is the richest structured source (verified live): "Physically
    Unable to Perform" / "Injured Reserve" / "Suspended" distinguish PUP vs IR vs
    suspension. But PUP/IR-STATUS rows are filtered out of the players table at ingest
    (sync keeps Active/Inactive), so the in-DB realistic signal is a rostered player
    carrying ``injury_status`` == "IR" (a multi-week absence) — mapped here too.
    Q/D/O day-to-day do NOT prorate (they play)."""
    s = (status or "").strip().lower()
    iy = (injury_status or "").strip().lower()
    if s in ("physically unable to perform", "pup", "pup-r"):
        return "pup"
    if s in ("suspended", "suspension") or iy in ("sus", "susp", "suspended"):
        return "suspension"
    # Structured IR — from the raw status OR a rostered player's canonical IR badge.
    if s in ("injured reserve", "ir", "ir-r") or iy == "ir":
        return "ir_long"
    # No structured multi-week absence (Active/Inactive/Practice Squad, or day-to-day).
    return None
