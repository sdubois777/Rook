"""
Canonical player resolution — the ONE place name-matching guards live.

Resolution is ID-FIRST (deterministic, exact) and only falls to a GUARDED name
match as a last resort. This module owns the pure guard (``guarded_name_pick``)
that the #217 fix requires — first-name agreement, last-name-only collision
REFUSAL, prominence ranking, loud-warn — so no site can reintroduce a naive
``candidates[0]``. The orchestrator that tries the stable IDs then this guard is
``PlayerRepository.resolve_player`` (DB-bound); this module stays pure so the guard
is unit-testable and shared (roster resolution + news attribution both call it).

Before this, the guard was reinvented per site (news resolver = guarded; roster
``find_by_name_fuzzy`` = UNGUARDED ``candidates[0]``; stats matcher = ID-first).
Consolidating means the guard is fixed once.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


def _norm(name: str) -> str:
    """Suffix-stripped, lowercased canonical form (reuses the one _norm_name)."""
    from backend.agents.roster_changes import _norm_name

    return _norm_name(name or "")


def name_match_tier(query_name: str, cand_name: str) -> int:
    """How well a candidate matches the query name (lower = stronger). The last-name
    is shared across collision candidates, so the FIRST name is what disambiguates —
    without it "A.J. Brown" resolves to the most prominent Brown (the #217 bug).
      0 = suffix-normalized FULL name equal ("Chris Godwin" == "Chris Godwin Jr.")
      1 = same last name + same first initial AND all remaining tokens agree
          ("M. Evans" vs "Mike Evans"; but NOT "A. Brown" vs "Amon-Ra St. Brown")
      2 = last name only, first names DISAGREE — a different person (REFUSED)
    """
    a, c = _norm(query_name), _norm(cand_name)
    if a and a == c:
        return 0
    at, ct = a.split(), c.split()
    if not at or not ct:
        return 2
    # First initial agrees AND the FULL remaining-token last name agrees. Using every
    # token after the first (not just the final token) guards the "A. Brown" vs
    # "Amon-Ra St. Brown" collision while still matching "M. Evans" -> "Mike Evans".
    if at[0][:1] == ct[0][:1] and at[1:] == ct[1:]:
        return 1
    return 2


def _prominence_key(p, team: Optional[str]):
    """Tiebreak (ascending = better) among SAME-tier candidates. Prominence first;
    team is the LOWEST tiebreak (canonical rows can carry stale team values, so the
    article/roster team must never override a strong anchored match).
      1. sleeper_id present (anchored/active over stale unanchored)
      2. tier (lower = more prominent; None last)
      3. bid ceiling (prominence proxy; higher = better)
      4. team match (lowest-priority tiebreak)
    """
    has_sleeper = 0 if getattr(p, "sleeper_id", None) else 1
    tier = p.tier if getattr(p, "tier", None) is not None else 99
    ceiling = -(getattr(p, "recommended_bid_ceiling", None)
                or getattr(p, "ai_bid_ceiling", None) or 0)
    team_miss = 0 if (
        team and getattr(p, "team_abbr", None) and p.team_abbr.upper() == team.upper()
    ) else 1
    return (has_sleeper, tier, ceiling, team_miss)


def guarded_name_pick(
    candidates: Sequence,
    name: Optional[str],
    *,
    team: Optional[str] = None,
    position: Optional[str] = None,
):
    """Pick the best Player for ``name`` from ``candidates`` under the #217 guard,
    or return None (loud-warned) rather than risk a wrong same-surname attribution.

    NEVER returns an unverified ``candidates[0]``: a last-name-only collision (no
    first-name agreement) is REFUSED. ``position`` (if given) filters candidates to
    the same position first — a hard cross-position guard. Every non-match logs
    loudly, never silent.
    """
    if not name:
        return None
    pool = list(candidates)
    if position:
        pu = position.upper()
        pool = [p for p in pool if (getattr(p, "position", None) or "").upper() == pu]
    if not pool:
        logger.warning(
            "resolve: no eligible candidate for name=%r team=%r pos=%r — NOT resolved",
            name, team, position,
        )
        return None

    eligible = [(name_match_tier(name, getattr(p, "name", "")), p) for p in pool]
    eligible = [(mt, p) for mt, p in eligible if mt <= 1]  # refuse tier 2 (last-only)
    if not eligible:
        logger.warning(
            "resolve: last-name-only collision for name=%r team=%r among %r — REFUSED "
            "(no first-name match; safer to lose the signal than mis-attribute)",
            name, team, [getattr(p, "name", "?") for p in pool][:6],
        )
        return None

    eligible.sort(key=lambda mp: (mp[0], *_prominence_key(mp[1], team)))
    best = eligible[0][1]
    if len(eligible) > 1:
        logger.info(
            "resolve: %r/%r -> %r (sleeper_id=%s tier=%s); rejected %s",
            name, team, best.name, getattr(best, "sleeper_id", None),
            getattr(best, "tier", None),
            [(p.name, getattr(p, "tier", None)) for _, p in eligible[1:5]],
        )
    return best
