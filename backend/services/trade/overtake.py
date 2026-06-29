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
    """Result of the §4 guard: ``passes`` is True when my post-trade starting
    strength stays ≥ theirs. The two strengths are surfaced for slice 4 / debug."""
    passes: bool
    my_strength: float
    their_strength: float


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
    """§4 condition 4: after the trade, does my starting-lineup strength stay
    ≥ theirs? Passes when I don't fall behind on the field — even if the trade
    looked good on raw player value. (Equal strengths pass: the bar is ≥.)"""
    post = apply_trade(my_roster, their_roster, give_ids, get_ids)
    my_strength = roster_strength(list(post.my_roster), rules)
    their_strength = roster_strength(list(post.their_roster), rules)
    return OvertakeResult(
        passes=my_strength >= their_strength,
        my_strength=my_strength,
        their_strength=their_strength,
    )
