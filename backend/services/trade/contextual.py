"""
contextual_value — what a player is worth TO a specific roster (slice 2 of the
trade acceptability model, docs/trade_acceptability_design.md §2.2).

Unlike the intrinsic ``forward_value`` (same number for everyone), contextual
value is roster-relative: a startable RB is worth a lot to an RB-thin team (he
becomes a starter) and little to an RB-rich team (he rides the bench). That
asymmetry is what makes positive-sum (mutually-acceptable) trades representable.

  contextual_value(player, roster) = max(starter_upgrade, bench_depth_value)

  • starter_upgrade — the player's marginal contribution to the roster's OPTIMAL
    lineup (reuses the slice-1 primitive, displacement-aware): strength(roster +
    player) − strength(roster). This is automatically "his forward_value minus
    the weakest starter he displaces" when he cracks the lineup (incl. via FLEX),
    and 0 when he wouldn't start.
  • bench_depth_value — a non-starter still has depth/insurance value, scaled by
    how CLOSE he is to starting (vs the weakest dedicated starter at his
    position), steep so a genuine sit is worth much less than a near-starter and
    a deep-bench piece bottoms out near zero.

``roster`` is the roster the player is being valued FOR and does NOT contain him
(we measure the value of ADDING him). Pure function over the value bundle.
"""
from __future__ import annotations

from typing import Optional

from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    LineupRules,
    optimal_lineup,
)

# --- bench-depth (Fork 1a) — marginal-curve steepness, tunable in one place ---
# Fraction of a non-starter's forward_value retained when he's right at the
# start line, and the steepness with which that decays as he falls below it.
# Steep (exponent > 1): a genuine sit is worth much less than a near-starter.
_BENCH_DEPTH_WEIGHT = 0.5      # value retained at the start line (closeness == 1)
_BENCH_DEPTH_STEEPNESS = 2.0   # exponent on closeness; higher = steeper drop-off


def contextual_value(
    player: LineupPlayer,
    roster: list[LineupPlayer],
    rules: Optional[LineupRules] = None,
) -> float:
    """Value of ``player`` TO ``roster`` (roster excludes the player). Roster-
    relative: judged against this roster's optimal lineup + its starters at the
    player's position, never a global slot count."""
    rules = rules or DEFAULT_LINEUP_RULES

    # 1. Starter upgrade — marginal contribution to the optimal lineup.
    base = optimal_lineup(roster, rules).strength
    with_player = optimal_lineup([*roster, player], rules).strength
    starter_upgrade = round(with_player - base, 1)  # ≥ 0

    # 2. Bench-depth value — closeness to the weakest DEDICATED starter at his
    #    position on this roster (FLEX-cracking is already captured by the
    #    upgrade term above; this only binds when he wouldn't start at all).
    k = rules.slots.get(player.position, 0)
    same_pos = sorted(
        (p.forward_value for p in roster if p.position == player.position),
        reverse=True,
    )
    if k >= 1 and len(same_pos) >= k and same_pos[k - 1] > 0:
        weakest_pos_starter = same_pos[k - 1]
        closeness = min(1.0, player.forward_value / weakest_pos_starter)
    else:
        # Roster thin at his position → he'd start; the upgrade term governs.
        closeness = 1.0
    bench_depth_value = round(
        _BENCH_DEPTH_WEIGHT * player.forward_value * (closeness ** _BENCH_DEPTH_STEEPNESS),
        1,
    )

    return max(starter_upgrade, bench_depth_value)
