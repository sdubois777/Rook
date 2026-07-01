"""
Trade application + the overtake guard (slice 3 of the trade acceptability model,
docs/trade_acceptability_design.md §4 / condition 4).

This is the first place a trade is modeled as an OPERATION on two rosters rather
than a scalar delta — slice 4's edge band reuses ``apply_trade``. The overtake
guard is §4 / condition 4 ONLY: after the trade, is my starting-lineup strength
still ≥ theirs? (Conditions 1-3 and the proposal/analyzer wiring are slice 4.)

Built on the slice-1 ``optimal_lineup`` / ``roster_strength`` primitives — no
lineup logic reimplemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.services.trade.lineup import LineupPlayer, LineupRules, roster_strength


class TradeError(ValueError):
    """A trade references players that aren't on the rosters they'd be leaving."""


@dataclass(frozen=True)
class PostTrade:
    """The two rosters AFTER a trade (inputs are never mutated)."""
    my_roster: tuple[LineupPlayer, ...]
    their_roster: tuple[LineupPlayer, ...]


@dataclass(frozen=True)
class OvertakeResult:
    """Result of the §4 guard. ``passes`` is True unless the trade FLIPS me from
    ahead-or-tied to behind (a trade-caused overtake) — it is a RELATIVE,
    before→after test, not an absolute "must end up ahead" bar. ``my_strength`` /
    ``their_strength`` are the POST-trade strengths (callers rely on this); the
    ``*_pre`` fields surface the pre-trade strengths so the verdict/analyzer can
    explain c4 honestly."""
    passes: bool
    my_strength: float          # post-trade (mine)
    their_strength: float       # post-trade (theirs)
    my_strength_pre: float
    their_strength_pre: float


def apply_trade(
    my_roster: list[LineupPlayer],
    their_roster: list[LineupPlayer],
    give_ids: list[str],
    get_ids: list[str],
) -> PostTrade:
    """Apply a trade: ``give_ids`` move me→them, ``get_ids`` move them→me. Pure —
    returns new rosters, mutates neither input. Raises ``TradeError`` if a give
    player isn't on my roster or a get player isn't on theirs."""
    mine_by = {p.player_id: p for p in my_roster}
    theirs_by = {p.player_id: p for p in their_roster}

    bad_give = [g for g in give_ids if g not in mine_by]
    bad_get = [g for g in get_ids if g not in theirs_by]
    if bad_give:
        raise TradeError(f"give players not on your roster: {bad_give}")
    if bad_get:
        raise TradeError(f"get players not on the other roster: {bad_get}")

    give_set, get_set = set(give_ids), set(get_ids)
    new_mine = [p for p in my_roster if p.player_id not in give_set] + [theirs_by[g] for g in get_ids]
    new_theirs = [p for p in their_roster if p.player_id not in get_set] + [mine_by[g] for g in give_ids]
    return PostTrade(my_roster=tuple(new_mine), their_roster=tuple(new_theirs))


def overtake_guard(
    my_roster: list[LineupPlayer],
    their_roster: list[LineupPlayer],
    give_ids: list[str],
    get_ids: list[str],
    rules: Optional[LineupRules] = None,
) -> OvertakeResult:
    """§4 condition 4 — NO-OVERTAKE-ONLY (relative, trade-caused). The guard FAILS
    iff the trade flips me from AHEAD-OR-TIED to BEHIND on the field:

        was_ahead_or_tied = my_pre  >= their_pre
        is_behind_after   = my_post <  their_post
        passes            = NOT (was_ahead_or_tied AND is_behind_after)

    Consequences (intended): if I was ALREADY BEHIND pre-trade I have no lead to
    surrender, so c4 cannot fail — a weaker team trades up freely. A leader may
    narrow their own lead and still pass (conditions 1-3 already stop them being
    fleeced on the value of the trade itself). c4 blocks ONLY the specific case of
    a trade that hands the other side a lead they didn't have."""
    my_pre = roster_strength(list(my_roster), rules)
    their_pre = roster_strength(list(their_roster), rules)
    post = apply_trade(my_roster, their_roster, give_ids, get_ids)
    my_post = roster_strength(list(post.my_roster), rules)
    their_post = roster_strength(list(post.their_roster), rules)

    was_ahead_or_tied = my_pre >= their_pre
    is_behind_after = my_post < their_post
    return OvertakeResult(
        passes=not (was_ahead_or_tied and is_behind_after),
        my_strength=my_post,
        their_strength=their_post,
        my_strength_pre=my_pre,
        their_strength_pre=their_pre,
    )
