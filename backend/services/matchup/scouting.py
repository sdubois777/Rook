"""H2H matchup scouting primitives — pure and deterministic.

Everything here is a plain function over rosters + already-computed InSeasonValue /
LineupPlayer bundles. It NEVER invokes Sonnet, credits, or the AI pipeline (the
funnel constraint). The metered trade-finder stays behind its paywall; this module
only produces the free scouting facts that MOTIVATE a handoff to it.

Three concerns:
  * ``synthesize_week_matchups`` — a deterministic round-robin (circle method) over
    the league's teams for one week, populated into the PERMANENT ``WeeklyMatchup``
    shape (platform_models.py) so a real ``get_matchups`` provider later drops in
    with no new model. No fantasy schedule exists in demo OR live today (get_matchups
    is a stub), so the demo synthesizes one.
  * ``positional_slot_ppg`` — per-position projected points decomposed by OPTIMAL-
    LINEUP SLOT (startable contribution only; FLEX attributes to the position that
    fills it). Sums EXACTLY to ``lineup_strength_ppg`` — the cross-page invariant.
  * ``win_prob_band`` — an APPROXIMATE qualitative band from the projected margin.
    InSeasonValue has no per-player variance, so a calibrated % would fabricate rigor
    we don't have (the K/DEF-streaming honesty discipline). Margin is the headline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.integrations.platform_models import WeeklyMatchup
from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    LineupRules,
    _slot_pos,
    optimal_lineup,
)
from backend.services.trade.value_engine import Confidence

logger = logging.getLogger(__name__)

# Position order for the battle grid (offense first, then K/DST).
GRID_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DEF")

# A bench body is genuine tradeable DEPTH (surplus) only if his forward_ppg clears
# the position's replacement floor by at least this margin. Modest by design — it
# drops at/below-replacement dead weight (a 0-ppg IR body isn't "spare") while
# keeping a real streamable-plus reserve. Tunable in one place.
SURPLUS_MARGIN_PPW: float = 1.0

# Margin bands (projected ppw difference, acting-team perspective). Deliberately
# WIDE — a ~1.5 ppw edge is a toss-up, not a coin-flip dressed as a percentage.
_TOSSUP_PPW = 3.0
_SLIGHT_PPW = 9.0
_CLEAR_PPW = 20.0


# ---------------------------------------------------------------------------
# Schedule synthesis (deterministic round-robin, permanent WeeklyMatchup shape)
# ---------------------------------------------------------------------------
def synthesize_week_matchups(team_ids: list[str], week: int) -> list[WeeklyMatchup]:
    """Deterministic circle-method pairing for ``week`` over ``team_ids`` (order is
    the seed → same pairing on every re-run). Populates the PERMANENT WeeklyMatchup
    shape (scores 0, is_complete False — it's a forward preview, not a played game).

    Every team gets exactly one opponent. An odd count can't pair evenly — loud-warn
    and leave the byed team out (never silently mis-pair).
    """
    ids = list(team_ids)
    n = len(ids)
    if n < 2:
        logger.warning("synthesize_week_matchups: <2 teams (%d) — no matchups for week %d", n, week)
        return []
    byed: Optional[str] = None
    if n % 2 == 1:
        # Circle method needs an even count; the last team in rotation sits out.
        byed = ids[-1]
        logger.warning(
            "synthesize_week_matchups: ODD team count (%d) for week %d — %r has a BYE "
            "(uneven pairing, not a silent drop)", n, week, byed,
        )
        ids = ids[:-1]
        n -= 1

    fixed, rot = ids[0], ids[1:]
    rounds = n - 1
    r = (week - 1) % rounds
    rot = rot[r:] + rot[:r]            # rotate the non-fixed teams by the round index
    arranged = [fixed] + rot

    matchups: list[WeeklyMatchup] = []
    for i in range(n // 2):
        home, away = arranged[i], arranged[n - 1 - i]
        matchups.append(WeeklyMatchup(
            week=week, home_team_id=home, away_team_id=away,
            home_score=0.0, away_score=0.0, is_complete=False,
        ))

    paired = {t for m in matchups for t in (m.home_team_id, m.away_team_id)}
    expected = set(team_ids) - ({byed} if byed else set())
    if paired != expected:
        logger.warning(
            "synthesize_week_matchups: pairing covered %d of %d teams for week %d "
            "(missing: %s)", len(paired), len(expected), week, sorted(expected - paired),
        )
    return matchups


def opponent_of(matchups: list[WeeklyMatchup], team_id: str) -> Optional[str]:
    """The team_id ``team_id`` faces this week, or None (bye / not scheduled)."""
    for m in matchups:
        if m.home_team_id == team_id:
            return m.away_team_id
        if m.away_team_id == team_id:
            return m.home_team_id
    return None


# ---------------------------------------------------------------------------
# Positional battle grid (by optimal-lineup slot; sums to lineup_strength_ppg)
# ---------------------------------------------------------------------------
def positional_slot_ppg(
    roster: list[LineupPlayer],
    rules: Optional[LineupRules] = None,
    replacement_ppg: Optional[dict[str, float]] = None,
) -> dict[str, float]:
    """Per-position projected ppw from the OPTIMAL STARTING lineup — startable
    contribution only. A filled slot credits its player's actual position (so a FLEX
    lands on the position that fills it); an unfilled required slot credits the
    position's replacement floor, exactly like ``lineup_strength_ppg``. The returned
    dict therefore SUMS to ``lineup_strength_ppg(roster, rules, replacement_ppg)`` —
    the invariant the H2H margin and the ladder share."""
    rules = rules or DEFAULT_LINEUP_RULES
    ol = optimal_lineup(roster, rules)
    by_pid = {p.player_id: p for p in roster}
    grid: dict[str, float] = {pos: 0.0 for pos in GRID_POSITIONS}

    for label, pid in ol.slots:
        if pid is not None:
            p = by_pid[pid]
            grid[p.position] = grid.get(p.position, 0.0) + p.forward_ppg
            continue
        # Unfilled slot → replacement floor (0 without a replacement map, matching
        # lineup_strength_ppg's pre-fix behavior).
        if not replacement_ppg:
            continue
        pos = _slot_pos(label)
        if pos == "FLEX":
            # The easiest-to-stream flex position (highest replacement), same as
            # lineup_strength_ppg — credited to that position's bucket.
            elig = [(fp, replacement_ppg.get(fp, 0.0)) for fp in rules.flex_positions]
            if elig:
                best_pos, best_val = max(elig, key=lambda x: x[1])
                grid[best_pos] = grid.get(best_pos, 0.0) + best_val
        else:
            grid[pos] = grid.get(pos, 0.0) + replacement_ppg.get(pos, 0.0)

    return {pos: round(grid.get(pos, 0.0), 2) for pos in GRID_POSITIONS}


# ---------------------------------------------------------------------------
# Win-prob band (APPROXIMATE — margin-derived, no fabricated percentage)
# ---------------------------------------------------------------------------
# A lineup is "thin" (band-widening) only when a MEANINGFUL SHARE of its starters
# are non-full — a single thin K on one side must not collapse an otherwise-
# confident margin to a toss-up (verified: only ~4% of demo starters are non-full,
# so the old min-rule made every matchup low-confidence). One-third is the bar.
_THIN_SHARE = 1.0 / 3.0


def confidence_summary(starters_confidences: list[Confidence]) -> tuple[str, bool]:
    """(note, low_confidence) from the combined starters' confidences. Share-based,
    not min-based: ``low_confidence`` is True only when >= 1/3 of starters are
    non-full. The note is qualitative (full / mostly_full / thin) — used to widen
    the toss-up band, never to manufacture a number."""
    if not starters_confidences:
        return ("thin", True)
    n = len(starters_confidences)
    non_full = sum(1 for c in starters_confidences if c.value != "full")
    share = non_full / n
    if share == 0.0:
        return ("full", False)
    if share < _THIN_SHARE:
        return ("mostly_full", False)
    return ("thin", True)


# ---------------------------------------------------------------------------
# Trade-leverage readout — VALUE-GATED surplus + RECONCILED need + RECIPROCAL fit
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Leverage:
    """The Matchup trade-leverage readout — value-aware, so the flag MEANS something.

    A position is SURPLUS only when a bench body clears replacement by
    ``SURPLUS_MARGIN_PPW`` AND the position isn't itself a need (need wins → a
    position never renders in BOTH lists). The mirror flag requires a genuine
    RECIPROCAL value-fit in BOTH directions; otherwise there is no clear fit and
    the surface says so plainly (don't fabricate leverage that isn't there)."""
    my_needs: tuple[str, ...]
    opp_needs: tuple[str, ...]
    my_surplus_positions: tuple[str, ...]          # value-gated tradeable depth
    opp_surplus_positions: tuple[str, ...]
    their_surplus_my_needs: tuple[str, ...]         # their real depth ∩ my need
    my_surplus_their_needs: tuple[str, ...]         # my real depth ∩ their need
    is_reciprocal_fit: bool                          # both directions non-empty → mirror


def value_gated_surplus_positions(
    surplus_ids,
    needs,
    values: dict,
    replacement: dict[str, float],
    margin: float = SURPLUS_MARGIN_PPW,
) -> list[str]:
    """Positions where the team has GENUINE tradeable depth: a bench body whose
    forward_ppg exceeds the position's replacement floor by ``margin``, at a
    position that is NOT a need (a weak/thin position's bench bodies aren't spare —
    need wins the reconciliation, so no position lands in both lists)."""
    need_set = set(needs)
    out: set[str] = set()
    for pid in surplus_ids:
        v = values.get(pid)
        if v is None:
            continue
        pos = v.position
        if pos in need_set:
            continue  # need wins — its depth isn't "spare"
        if v.forward_ppg > replacement.get(pos, 0.0) + margin:
            out.add(pos)
    return [p for p in GRID_POSITIONS if p in out]


def leverage_readout(
    my_needs,
    my_surplus_ids,
    opp_needs,
    opp_surplus_ids,
    values: dict,
    replacement: dict[str, float],
    margin: float = SURPLUS_MARGIN_PPW,
) -> Leverage:
    """Build the value-gated, reconciled, reciprocal leverage readout for one
    matchup. ``*_needs`` / ``*_surplus_ids`` come from the SHARED analyze_roster
    (unchanged); the value-awareness lives HERE, on the Matchup surface only."""
    my_surp = value_gated_surplus_positions(my_surplus_ids, my_needs, values, replacement, margin)
    opp_surp = value_gated_surplus_positions(opp_surplus_ids, opp_needs, values, replacement, margin)

    their_surplus_my_needs = [p for p in GRID_POSITIONS if p in set(opp_surp) & set(my_needs)]
    my_surplus_their_needs = [p for p in GRID_POSITIONS if p in set(my_surp) & set(opp_needs)]
    # A genuine mirror requires a two-sided value-fit — each side's REAL depth
    # covers the other's need. A single-direction overlap is not a mirror.
    is_reciprocal = bool(their_surplus_my_needs) and bool(my_surplus_their_needs)

    return Leverage(
        my_needs=tuple(sorted(my_needs)),
        opp_needs=tuple(sorted(opp_needs)),
        my_surplus_positions=tuple(my_surp),
        opp_surplus_positions=tuple(opp_surp),
        their_surplus_my_needs=tuple(their_surplus_my_needs),
        my_surplus_their_needs=tuple(my_surplus_their_needs),
        is_reciprocal_fit=is_reciprocal,
    )


def win_prob_band(margin: float, low_confidence: bool = False) -> str:
    """An APPROXIMATE qualitative edge from the projected ppw margin (acting-team
    perspective). NOT a calibrated probability — InSeasonValue carries no variance,
    so a hard % would imply rigor we don't have. Under low confidence, a slight edge
    collapses to a toss-up (don't over-claim on thin data)."""
    m = margin
    a = abs(m)
    if a <= _TOSSUP_PPW or (low_confidence and a <= _SLIGHT_PPW):
        return "Toss-up"
    if a <= _SLIGHT_PPW:
        return "Slight edge" if m > 0 else "Slight underdog"
    if a <= _CLEAR_PPW:
        return "Favored" if m > 0 else "Underdog"
    return "Heavy favorite" if m > 0 else "Heavy underdog"
