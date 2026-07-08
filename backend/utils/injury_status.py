"""
Canonical injury-status vocabulary — the single normalization point.

Live injury feeds use inconsistent strings (Sleeper: Questionable / Out / IR /
DNR / NA, no "Doubtful"; nflverse: Questionable / Out / Doubtful / Note). The
BADGE and any FUTURE value map key off THIS canonical set, never raw source
strings.

Canonical codes (stored on Player.injury_status; None = healthy / no badge):
    "Q"  QUESTIONABLE
    "D"  DOUBTFUL          (nflverse today; Sleeper folds into Q)
    "O"  OUT               (ruled out this game)
    "IR" INJURED_RESERVE   (multi-week; PUP folded here for display)

Non-injury statuses (DNR / NA / suspension / active) map to None — no badge.
Anything genuinely unrecognized LOUD-WARNs (never a silent drop) and returns None.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Canonical codes.
QUESTIONABLE = "Q"
DOUBTFUL = "D"
OUT = "O"
IR = "IR"
CANONICAL: frozenset[str] = frozenset({QUESTIONABLE, DOUBTFUL, OUT, IR})

# Raw (lowercased) → canonical code.
_TO_CANONICAL: dict[str, str] = {
    "questionable": QUESTIONABLE,
    "q": QUESTIONABLE,
    "doubtful": DOUBTFUL,
    "d": DOUBTFUL,
    "out": OUT,
    "o": OUT,
    "ir": IR,
    "injured reserve": IR,
    "injured_reserve": IR,
    "pup": IR,            # physically-unable-to-perform ~ multi-week, bucket to IR for display
}

# Known statuses that are NOT a game-injury designation → no badge, NO warning.
_KNOWN_NONE: frozenset[str] = frozenset({
    "dnr", "na", "n/a", "", "none", "healthy", "active", "probable",
    "sus", "susp", "suspension", "suspended", "cov", "covid", "nfi",
})


def to_canonical(raw: Optional[str]) -> Optional[str]:
    """Normalize a raw injury string to a canonical code, or None (healthy / not a
    game-injury designation). LOUD-WARNs on any genuinely unrecognized string
    (returns None) so a new designation surfaces instead of silently vanishing."""
    if raw is None:
        return None
    key = str(raw).strip().lower()
    if key in _TO_CANONICAL:
        return _TO_CANONICAL[key]
    if key in _KNOWN_NONE:
        return None
    logger.warning(
        "injury_status: unrecognized designation %r -> no badge "
        "(add it to backend/utils/injury_status.py if it should show)", raw,
    )
    return None
