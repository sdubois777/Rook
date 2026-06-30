"""
Deterministic trade-verdict computation — the grounded core the analyzer agent
explains (it does NOT let the LLM pick the winner).

The verdict is computed in Python purely from the value engine's per-player
``forward_value`` + confidence signals, so:
  * it can never reintroduce the name bias the engine stripped out (the winner is
    a function of engine value, not the player's name/reputation), and
  * it honours confidence: a trade involving a limited/insufficient/team-change
    player is HEDGED (never a crisp "lopsided" verdict) — the engine already
    neutralised those flags and the verdict must not re-manufacture certainty.

The Sonnet agent (backend/agents/trade_analyzer.py) consumes this and writes the
human-readable rationale; it must not override the winner.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.services.trade.league_state import LeagueState
from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    fit_to_limit,
    lineup_strength_ppg,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend

# Verdict is the change in your STARTING LINEUP's points/week on the RESULTING
# roster (trade_lineup_value_design.md §7), NOT summed forward_value. Thresholds
# in lineup ppg: within the maintains-tolerance reads "even"; a gain at/above the
# value threshold reads "lopsided", between is a "lean".
_MAINTAINS_TOLERANCE = 0.5    # |Δlineup| within this → even / barely moves
_LINEUP_GAIN_THRESHOLD = 5.0  # |Δlineup| at/above this → lopsided; between → lean

_CONF_RANK = {Confidence.FULL: 2, Confidence.LIMITED: 1, Confidence.INSUFFICIENT: 0}

DEFAULT_ROSTER_LIMIT = 16


class TradeValidationError(ValueError):
    """Raised when the trade references unknown teams/players or is empty."""


@dataclass
class PlayerGrounding:
    canonical_player_id: str
    name: str
    position: str
    side: str            # "give" | "get"
    forward_value: float
    value_trend: str
    confidence: str
    buy_low: bool
    sell_high: bool
    why: str


@dataclass
class RosterGuard:
    triggered: bool
    net_players: int
    open_slots: int
    drop_recommendations: list[dict]   # [{id, name, forward_value}]
    message: str


@dataclass
class TradeAnalysis:
    my_team_id: str
    give: list[PlayerGrounding]
    get: list[PlayerGrounding]
    give_value: float
    get_value: float
    value_delta: float            # get − give of raw forward_value (grounding only; does NOT drive the verdict)
    lineup_gain: float            # HEADLINE: Δ your starting-lineup points/week (resulting roster, net of drops)
    winner: str                   # "you" | "opponent" | "even" — from lineup_gain
    fairness: str                 # fair | lean you/opponent | lopsided you/opponent
    confidence: str               # floor confidence across involved players
    hedged: bool
    hedge_reason: str
    roster_guard: RosterGuard
    rationale: str = ""


def _grounding(v: InSeasonValue, side: str) -> PlayerGrounding:
    return PlayerGrounding(
        canonical_player_id=v.canonical_player_id, name=v.name, position=v.position,
        side=side, forward_value=v.forward_value, value_trend=v.value_trend.value,
        confidence=v.confidence.value, buy_low=v.buy_low, sell_high=v.sell_high,
        why=v.why,
    )


def validate_trade(
    state: LeagueState,
    values: dict[str, InSeasonValue],
    my_team_id: str,
    give_ids: list[str],
    get_ids: list[str],
) -> None:
    """Cheap pre-deduction validation: teams/players resolve and the trade is a
    real two-sided trade. Raises TradeValidationError (→ 400) on any problem."""
    my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
    if my_team is None:
        raise TradeValidationError(f"team {my_team_id!r} not in league")
    if not give_ids or not get_ids:
        raise TradeValidationError("a trade needs at least one player on each side")

    my_ids = {rp.canonical_player_id for rp in my_team.roster}
    other_ids = state.all_rostered_player_ids() - my_ids

    for pid in give_ids:
        if pid not in my_ids:
            raise TradeValidationError(f"give player {pid!r} is not on your team")
        if pid not in values:
            raise TradeValidationError(f"no value for give player {pid!r}")
    for pid in get_ids:
        if pid not in other_ids:
            raise TradeValidationError(f"get player {pid!r} is not on another team")
        if pid not in values:
            raise TradeValidationError(f"no value for get player {pid!r}")


def _roster_guard(
    state: LeagueState,
    values: dict[str, InSeasonValue],
    my_team_id: str,
    give_ids: list[str],
    get_ids: list[str],
    roster_limit: int,
) -> RosterGuard:
    """The ONE locked roster rule: lineup legality is NOT enforced, but if the
    user nets more players than they give and lacks open slots, flag it and
    recommend the lowest-value droppable players."""
    my_team = next(t for t in state.teams if t.team_id == my_team_id)
    roster = list(my_team.roster)
    net = len(get_ids) - len(give_ids)
    open_slots = max(0, roster_limit - len(roster))
    size_after = len(roster) - len(give_ids) + len(get_ids)
    overflow = size_after - roster_limit

    if overflow <= 0:
        return RosterGuard(False, net, open_slots, [], "")

    # Drop candidates = current roster minus the players being traded away,
    # ranked by ascending forward_value (drop the least valuable first).
    keep = [rp for rp in roster if rp.canonical_player_id not in set(give_ids)]
    ranked = sorted(
        keep,
        key=lambda rp: values[rp.canonical_player_id].forward_value
        if rp.canonical_player_id in values else 0.0,
    )
    drops = ranked[:overflow]
    recs = [
        {
            "id": rp.canonical_player_id, "name": rp.name,
            "forward_value": values[rp.canonical_player_id].forward_value
            if rp.canonical_player_id in values else 0.0,
        }
        for rp in drops
    ]
    names = ", ".join(r["name"] for r in recs)
    return RosterGuard(
        True, net, open_slots, recs,
        f"You receive {len(get_ids)} and give {len(give_ids)} (net +{net}) but have "
        f"{open_slots} open slot(s) — over by {overflow}. Drop to fit: {names}.",
    )


def analyze_trade(
    state: LeagueState,
    values: dict[str, InSeasonValue],
    my_team_id: str,
    give_ids: list[str],
    get_ids: list[str],
    *,
    roster_limit: int = DEFAULT_ROSTER_LIMIT,
) -> TradeAnalysis:
    """Compute the deterministic, engine-grounded verdict. Assumes the trade has
    already passed ``validate_trade``."""
    validate_trade(state, values, my_team_id, give_ids, get_ids)

    give = [_grounding(values[pid], "give") for pid in give_ids]
    get = [_grounding(values[pid], "get") for pid in get_ids]
    give_value = round(sum(g.forward_value for g in give), 1)
    get_value = round(sum(g.forward_value for g in get), 1)
    value_delta = round(get_value - give_value, 1)   # grounding only; NOT the verdict

    # HEADLINE (§7): the change in YOUR optimal STARTING LINEUP's points/week on the
    # RESULTING roster (incoming + outgoing + forced drops), evaluated ONCE — not a
    # per-player value sum. This is what "did the trade make my team better" means.
    my_team = next(t for t in state.teams if t.team_id == my_team_id)

    def _lp(pid: str) -> LineupPlayer:
        v = values[pid]
        return LineupPlayer(pid, v.position, v.forward_value, v.forward_ppg, bool(v.buy_low))

    give_set = set(give_ids)
    my_pre = [_lp(rp.canonical_player_id) for rp in my_team.roster if rp.canonical_player_id in values]
    my_post = fit_to_limit(
        [p for p in my_pre if p.player_id not in give_set] + [_lp(g) for g in get_ids],
        roster_limit,
    )
    lineup_gain = round(
        lineup_strength_ppg(my_post, DEFAULT_LINEUP_RULES)
        - lineup_strength_ppg(my_pre, DEFAULT_LINEUP_RULES), 2,
    )

    abs_g = abs(lineup_gain)
    if abs_g <= _MAINTAINS_TOLERANCE:
        winner, fairness = "even", "fair"
    else:
        side = "you" if lineup_gain > 0 else "opponent"
        fairness = f"lopsided {side}" if abs_g >= _LINEUP_GAIN_THRESHOLD else f"lean {side}"
        winner = side

    # Confidence floor + hedge: any non-full player softens the verdict.
    involved = [values[pid] for pid in (*give_ids, *get_ids)]
    floor = min(involved, key=lambda v: _CONF_RANK[v.confidence]).confidence
    thin = [v for v in involved if v.confidence is not Confidence.FULL]
    hedged = bool(thin)
    hedge_reason = "; ".join(
        f"{v.name}: {v.confidence.value} ({v.confidence_reason})"
        if v.confidence_reason else f"{v.name}: {v.confidence.value}"
        for v in thin
    )
    if hedged and fairness.startswith("lopsided"):
        # Don't assert a crisp blowout off thin/team-change data.
        fairness = fairness.replace("lopsided", "lean")

    guard = _roster_guard(state, values, my_team_id, give_ids, get_ids, roster_limit)

    return TradeAnalysis(
        my_team_id=my_team_id, give=give, get=get,
        give_value=give_value, get_value=get_value, value_delta=value_delta,
        lineup_gain=lineup_gain,
        winner=winner, fairness=fairness, confidence=floor.value,
        hedged=hedged, hedge_reason=hedge_reason, roster_guard=guard,
    )
