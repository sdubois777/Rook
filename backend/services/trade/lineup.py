"""
optimal_lineup — the shared lineup-optimization primitive (slice 1 of the trade
acceptability model, docs/trade_acceptability_design.md §2.1).

Given a roster (players with a position + forward_value) and the league's lineup
rules, it returns the best LEGAL starting lineup that maximizes total
forward_value, plus that lineup's strength. It is a pure function over the value
bundle — no LeagueState / DB coupling — so both ``contextual_value`` and the
overtake guard (later slices) can call it cheaply.

Optimality: for the standard structure of dedicated per-position slots plus K
FLEX spots drawn from a superset of positions, the greedy choice — fill each
dedicated slot with the best players of its position, then fill FLEX with the
best remaining eligible — is provably optimal. Dedicated slots always want the
top players at their position (any optimal lineup can be rearranged so without
loss), and a single/again-greedy FLEX then takes the best leftover. All
forward_values are ≥ 0, so filling more legal slots never hurts.

⚠️ OPTIMALITY BOUNDARY: the greedy is provably optimal ONLY for single-FLEX-
from-a-superset shapes (the demo + standard redraft). SUPERFLEX (a slot eligible
for QB *and* RB/WR/TE) and MULTI-FLEX with DISJOINT pools break the greedy proof
— greedy could silently return a plausible-but-suboptimal lineup. A real non-
single-flex league needs a proper assignment solver (e.g. max-weight bipartite
matching). No runtime guard today (nothing exercises non-single-flex); guard or
solve before a non-single-flex real league is in scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class LineupPlayer:
    """A roster entry as the optimizer needs it: a stable id (for deterministic
    tie-breaking), a position, and the player's forward_value."""
    player_id: str
    position: str
    forward_value: float


@dataclass(frozen=True)
class LineupRules:
    """League lineup shape. ``slots`` = dedicated per-position counts; ``flex_count``
    FLEX spots drawn from ``flex_positions``. Parameterized so real leagues differ
    from the demo without touching the algorithm."""
    slots: dict[str, int]
    flex_count: int
    flex_positions: tuple[str, ...]


# The demo league shape: 1 QB, 2 RB, 3 WR, 1 TE, 1 FLEX (RB/WR/TE).
DEFAULT_LINEUP_RULES = LineupRules(
    slots={"QB": 1, "RB": 2, "WR": 3, "TE": 1},
    flex_count=1,
    flex_positions=("RB", "WR", "TE"),
)


@dataclass(frozen=True)
class OptimalLineup:
    starters: tuple[LineupPlayer, ...]
    strength: float
    # (slot_label, player_id-or-None) for every slot, in slot order — empty slots
    # (degenerate/thin rosters) carry None.
    slots: tuple[tuple[str, Optional[str]], ...] = field(default_factory=tuple)


def _sort_key(p: LineupPlayer):
    # Highest forward_value first; ties broken by player_id ascending (stable,
    # deterministic across runs).
    return (-p.forward_value, p.player_id)


def optimal_lineup(
    roster: list[LineupPlayer],
    rules: Optional[LineupRules] = None,
) -> OptimalLineup:
    """Best legal starting lineup + its strength. Degenerate rosters (fewer
    players than slots, or none at a required position) fill what's legal and
    leave the rest empty — never crashing, never negative."""
    rules = rules or DEFAULT_LINEUP_RULES

    by_pos: dict[str, list[LineupPlayer]] = {}
    for p in roster:
        by_pos.setdefault(p.position, []).append(p)
    for plist in by_pos.values():
        plist.sort(key=_sort_key)

    used: set[str] = set()
    starters: list[LineupPlayer] = []
    slot_assign: list[tuple[str, Optional[str]]] = []

    # 1. Dedicated per-position slots — best players of each position.
    for pos, count in rules.slots.items():
        avail = [p for p in by_pos.get(pos, []) if p.player_id not in used]
        for i in range(count):
            label = f"{pos}{i + 1}" if count > 1 else pos
            if i < len(avail):
                p = avail[i]
                used.add(p.player_id)
                starters.append(p)
                slot_assign.append((label, p.player_id))
            else:
                slot_assign.append((label, None))  # thin at this position

    # 2. FLEX — best remaining FLEX-eligible players.
    flex_avail = sorted(
        (p for p in roster
         if p.position in rules.flex_positions and p.player_id not in used),
        key=_sort_key,
    )
    for i in range(rules.flex_count):
        label = f"FLEX{i + 1}" if rules.flex_count > 1 else "FLEX"
        if i < len(flex_avail):
            p = flex_avail[i]
            used.add(p.player_id)
            starters.append(p)
            slot_assign.append((label, p.player_id))
        else:
            slot_assign.append((label, None))

    strength = round(sum(p.forward_value for p in starters), 1)
    return OptimalLineup(
        starters=tuple(starters), strength=strength, slots=tuple(slot_assign),
    )


def roster_strength(roster: list[LineupPlayer], rules: Optional[LineupRules] = None) -> float:
    """A roster's team strength = its optimal STARTING-lineup strength (§4:
    fantasy is won by who you start). Thin wrapper over optimal_lineup; bench
    depth is explicitly a v2 refinement (see the design's deferred ledger)."""
    return optimal_lineup(roster, rules).strength
