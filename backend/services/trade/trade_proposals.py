"""
Trade proposals — the system finds trades. Surfacing is governed by the
four-condition EDGE BAND (trade_acceptability_design.md §3): a candidate surfaces
only if YOU improve, THEY improve comfortably, you keep the edge, and you don't
fall behind on the field. This replaces the old zero-sum ``winner == "you"`` bar,
which could only ever surface trades that were bad for the other side.

Every value is CONTEXTUAL (slice 2, roster-relative), so positive-sum trades are
representable; condition 4 reuses the slice-3 overtake guard. Candidate
enumeration is unchanged (exhaustive 1-for-1 + the advisory LLM pass). The
never-pad rule is preserved and STRENGTHENED — far fewer candidates clear four
conditions, so "no clear trade right now" is the common, correct outcome.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

from backend.services.trade.contextual import contextual_value
from backend.services.trade.league_state import LeagueState, TeamState
from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    LineupRules,
    fit_to_limit,
    lineup_strength_ppg,
    optimal_lineup,
)
from backend.services.trade.overtake import apply_trade, overtake_guard
from backend.services.trade.trade_analysis import (
    DEFAULT_ROSTER_LIMIT,
    TradeAnalysis,
    analyze_trade,
    validate_trade,
)
from backend.services.trade.value_engine import InSeasonValue

MAX_PROPOSALS = 5

# --- LINEUP-IMPROVEMENT OBJECTIVE (trade_lineup_value_design.md) --------------
# A trade is judged by the change in your STARTING LINEUP's projected points/week,
# computed ONCE on the RESULTING roster (incoming + outgoing + forced drops) — NOT
# by summing the values of the players involved (which double-counted multi-player
# gets and never debited drops). §4 two-clause value rule:
_LINEUP_GAIN_THRESHOLD = 5.0   # clause (a): ppg starting-lineup gain to have value
                               # (anchor LOW ~5; below this is dwarfed by weekly variance)
_MAINTAINS_TOLERANCE = 0.5     # clause (b): "maintains" = Δlineup >= -this (anti-churn)

# DEPRECATED (superseded by the lineup objective): the old contextual-value comfort
# epsilon. Kept only as the acceptability READ's marginal-band label; the GATE's
# "acceptable to them" is now "improves their lineup" (§9), not a value epsilon.
_COMFORT_THRESHOLD = 2.0

# Targeted-enumeration bounds (slice 6, §6d/§6e). HARD CAP 3 players per side
# (Stephen: past 3-for-3 reeks of desperation / nobody accepts). The matched-pool
# cap bounds the combinatorial space — only need/surplus-matched pieces are ever
# combined, and at most this many per side.
_MAX_PER_SIDE = 3
_MATCH_POOL_CAP = 5
# A dedicated starter weaker than this fraction of the team's MEDIAN starter is a
# fixable weakness → that position is a NEED (scale-free, relative to the roster).
_NEED_REL_FRACTION = 0.6
# A piece only joins a side's pool if it adds at least this much CONTEXTUAL value
# to the destination roster — i.e. it plausibly helps that side. Screens out
# deep-bench junk that would otherwise pad multi-player trades with zero-value
# extra players (same your_net/their_net, more bodies). Tunable.
_HELP_EPSILON = 1.0


@dataclass(frozen=True)
class Candidate:
    give_ids: tuple[str, ...]
    get_ids: tuple[str, ...]
    counterparty_team_id: str


def _candidate_key(c: "Candidate") -> tuple:
    """Order-INDEPENDENT trade identity: the give set, the get set, and the
    counterparty. ("a","b")->X and ("b","a")->X are the same trade."""
    return (frozenset(c.give_ids), frozenset(c.get_ids), c.counterparty_team_id)


def merge_candidates(*candidate_lists: list[Candidate]) -> list[Candidate]:
    """Union candidate lists from multiple generators (LLM + targeted enumerator),
    deduped on trade identity — the same trade from both sources counts once.
    Earlier lists win the slot, so pass the LLM's first to keep its ordering, then
    the enumerator's to add the trades the model missed."""
    seen: set[tuple] = set()
    out: list[Candidate] = []
    for lst in candidate_lists:
        for c in lst:
            key = _candidate_key(c)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


@dataclass(frozen=True)
class EdgeBand:
    """The §6 edge-band scoring of a candidate, in LINEUP points/week. Both sides
    are evaluated on their RESULTING roster (net of forced drops), once — not as a
    per-player value sum."""
    your_lineup_gain: float   # Δ my starting-lineup ppg (resulting roster, net of drops)
    their_lineup_gain: float  # Δ their starting-lineup ppg (resulting roster)
    my_strength: float        # post-trade 0-100 lineup strength (mine) — for the overtake guard
    their_strength: float     # post-trade 0-100 lineup strength (theirs)
    clears: bool


def _has_value(lineup_gain: float, gets_rising_bench: bool) -> bool:
    """§4 two-clause value rule (in lineup ppg). (a) a real starting-lineup upgrade,
    OR (b) the lineup is maintained AND an incoming bench piece is a rising/buy-low
    stash. A flat-usage bench add maintains the lineup but fails (b) → no value."""
    if lineup_gain >= _LINEUP_GAIN_THRESHOLD:
        return True                                              # clause (a)
    if lineup_gain >= -_MAINTAINS_TOLERANCE and gets_rising_bench:
        return True                                              # clause (b)
    return False


# Acceptability verdicts (the analyzer's opponent-side READ, §7). NOT a gate: any
# trade is evaluated and reported honestly — the verdict is whether the trade
# IMPROVES THEIR STARTING LINEUP (their resulting-roster lineup gain in ppg), not a
# value epsilon. "great for you, they'd reject it" surfaces as a rejection.
ACCEPT_LIKELY = "likely_accept"      # their_lineup_gain >= _LINEUP_GAIN_THRESHOLD
ACCEPT_MARGINAL = "marginal"         # 0 < their_lineup_gain < _LINEUP_GAIN_THRESHOLD
ACCEPT_REJECT = "likely_reject"      # their_lineup_gain <= 0 (their lineup doesn't improve)


@dataclass(frozen=True)
class Acceptability:
    """Would the OTHER side likely accept the trade the user built? A read derived
    from the edge band evaluated from the counterparty's perspective — their
    resulting-roster LINEUP gain (ppg) drives the verdict (§7: does it improve their
    lineup), the overtake guard adds the 'helps them more on the field' flag, and
    ``why`` is grounded in their roster. It is a READ, not a filter."""
    verdict: str
    their_lineup_gain: float  # their resulting-roster starting-lineup change (ppg)
    overtake_flag: bool       # the trade would make THEIR lineup overtake yours
    hedged: bool              # opponent-side data is limited/insufficient → soft read
    why: str


def _verdict_for(their_lineup_gain: float) -> str:
    if their_lineup_gain >= _LINEUP_GAIN_THRESHOLD:
        return ACCEPT_LIKELY
    if their_lineup_gain <= 0:
        return ACCEPT_REJECT
    return ACCEPT_MARGINAL


def _acceptability_why(
    values: dict[str, InSeasonValue],
    their_roster: list[LineupPlayer],
    give_ids: tuple[str, ...] | list[str],
    verdict: str,
    overtake_flag: bool,
    hedged: bool,
) -> str:
    """A one-line WHY grounded in THEIR roster. The give players (incoming to them)
    are valued against their roster as-is; the one worth most to them frames the
    sentence — fills a need (accept), modest upgrade (haggle), or no value (reject)."""
    def _cv(pid: str) -> float:
        v = values[pid]
        return contextual_value(LineupPlayer(pid, v.position, v.forward_value), their_roster)

    incoming = [g for g in give_ids if g in values]
    best = max(incoming, key=_cv) if incoming else None
    name = values[best].name if best else "what you're sending"
    pos = values[best].position if best else "that spot"

    if verdict == ACCEPT_LIKELY:
        why = f"{name} fills a {pos} need on their roster"
    elif verdict == ACCEPT_REJECT:
        why = f"they're set at {pos} — {name} adds little for them"
    else:
        why = f"{name} is a modest {pos} upgrade for them; they may haggle"
    if overtake_flag:
        why += "; it would also make their lineup stronger than yours"
    if hedged:
        why += " (tentative — limited data on a player involved)"
    return why


def acceptability_read(
    state: LeagueState,
    values: dict[str, InSeasonValue],
    my_team_id: str,
    give_ids: tuple[str, ...] | list[str],
    get_ids: tuple[str, ...] | list[str],
    *,
    hedged: bool,
    roster_limit: int = DEFAULT_ROSTER_LIMIT,
) -> Acceptability:
    """The §5/§6c analyzer read: evaluate the SAME trade from the counterparty's
    side and report whether they'd likely accept it. The counterparty is the team
    holding the ``get`` players (v1: a single counterparty — the team owning the
    first get player). Reuses ``evaluate_edge_band`` (which runs the overtake
    guard); never gates — on any failure it degrades to a safe, honest read."""
    my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
    get_set = set(get_ids)
    counterparty = next(
        (t for t in state.teams
         if t.team_id != my_team_id
         and any(rp.canonical_player_id in get_set for rp in t.roster)),
        None,
    )
    if my_team is None or counterparty is None:
        return Acceptability(ACCEPT_REJECT, 0.0, False, hedged,
                             "could not evaluate the other side of this trade")

    my_roster = _lineup_roster(my_team, values)
    their_roster = _lineup_roster(counterparty, values)
    try:
        edge = evaluate_edge_band(my_roster, their_roster, give_ids, get_ids, roster_limit=roster_limit)
    except Exception:
        return Acceptability(ACCEPT_REJECT, 0.0, False, hedged,
                             "could not evaluate the other side of this trade")

    overtake_flag = edge.my_strength < edge.their_strength   # guard failed → they overtake
    verdict = _verdict_for(edge.their_lineup_gain)
    why = _acceptability_why(values, their_roster, give_ids, verdict, overtake_flag, hedged)
    return Acceptability(verdict, edge.their_lineup_gain, overtake_flag, hedged, why)


@dataclass(frozen=True)
class RosterAnalysis:
    """Per-roster need/surplus (slice 6 BUILD 1, §6d) — the targeting primitive.
    SURPLUS = startable depth the lineup doesn't need (players outside the optimal
    starting lineup, so adding them back wouldn't improve it — low contextual
    value to their OWN team). NEED = positions the roster is thin/weak at."""
    team_id: str
    surplus_ids: tuple[str, ...]   # non-starters, highest forward_value first
    needs: frozenset[str]          # positions where the team is thin/weak


def _slot_position(label: str) -> str:
    """'WR2' -> 'WR', 'QB' -> 'QB', 'FLEX' -> 'FLEX'."""
    return "".join(ch for ch in label if not ch.isdigit())


def analyze_roster(
    team: TeamState,
    values: dict[str, InSeasonValue],
    rules: LineupRules | None = None,
) -> RosterAnalysis:
    """Derive need + surplus for one roster, reusing optimal_lineup (§6d). A player
    is SURPLUS if he's not in the optimal starting lineup (he doesn't improve it).
    A position is a NEED if a dedicated slot is empty (thin) or its weakest starter
    is weak relative to the team's own starters (a low-value starter to upgrade)."""
    rules = rules or DEFAULT_LINEUP_RULES
    roster = _lineup_roster(team, values)
    lineup = optimal_lineup(roster, rules)
    starter_ids = {p.player_id for p in lineup.starters}

    surplus = sorted(
        (p for p in roster if p.player_id not in starter_ids),
        key=lambda p: (-p.forward_value, p.player_id),
    )

    needs: set[str] = set()
    # Thin: a dedicated starting slot the roster couldn't fill.
    for label, pid in lineup.slots:
        pos = _slot_position(label)
        if pid is None and pos in rules.slots:
            needs.add(pos)
    # Weak: a dedicated starter well below the team's median starter value.
    starter_fvs = sorted(p.forward_value for p in lineup.starters)
    if starter_fvs:
        median = starter_fvs[len(starter_fvs) // 2]
        for pos in rules.slots:
            at_pos = [p.forward_value for p in lineup.starters if p.position == pos]
            if at_pos and min(at_pos) < _NEED_REL_FRACTION * median:
                needs.add(pos)

    return RosterAnalysis(
        team.team_id, tuple(p.player_id for p in surplus), frozenset(needs),
    )


def _can_fit(size: int, out_n: int, in_n: int, limit: int) -> bool:
    """Can a roster of ``size`` give ``out_n`` and receive ``in_n`` without
    overfilling past ``limit`` — possibly by dropping kept players? Receiving
    fewer than you give (in_n <= out_n) is always legal; otherwise the overflow
    must be absorbable by dropping non-traded players."""
    after = size - out_n + in_n
    if after <= limit:
        return True
    droppable = size - out_n          # kept players that could be dropped
    return (after - limit) <= droppable


def _both_sides_fit(
    my_size: int, opp_size: int, give_n: int, get_n: int, limit: int,
) -> bool:
    """Slot legality on BOTH sides (slice 6 BUILD 3, §6d). My side receives get_n
    and gives give_n; the opponent's the mirror. An uneven trade only the OTHER
    side can't fit is just as illegal as one I can't."""
    return (
        _can_fit(my_size, give_n, get_n, limit)
        and _can_fit(opp_size, get_n, give_n, limit)
    )


def enumerate_candidates(
    state: LeagueState,
    values: dict[str, InSeasonValue],
    my_team_id: str,
    *,
    roster_limit: int = DEFAULT_ROSTER_LIMIT,
    rules: LineupRules | None = None,
) -> list[Candidate]:
    """Need/surplus-TARGETED candidate generation (slice 6 BUILD 2, §6d/§6e),
    replacing the old exhaustive 1-for-1 enumeration. For each opponent, match MY
    surplus -> THEIR need and THEIR surplus -> MY need, then build trades that move
    surplus-for-need in BOTH directions: shapes 1-to-3 players per side, even AND
    uneven, HARD-capped at 3 (no 4+). Only need/surplus-matched pieces are ever
    combined and the matched pool is capped, so the candidate space stays bounded
    (orders of magnitude below all-subsets). The edge-band gate still judges every
    candidate — this only decides WHICH get generated, never whether they surface."""
    rules = rules or DEFAULT_LINEUP_RULES
    my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
    if my_team is None:
        return []
    me = analyze_roster(my_team, values, rules)
    my_size = len(my_team.roster)
    my_roster_lp = _lineup_roster(my_team, values)

    def _lp(pid: str) -> LineupPlayer:
        v = values[pid]
        return LineupPlayer(pid, v.position, v.forward_value)

    out: list[Candidate] = []
    for opp in state.teams:
        if opp.team_id == my_team_id:
            continue
        them = analyze_roster(opp, values, rules)
        their_roster_lp = _lineup_roster(opp, values)
        # MY surplus at a position THEY need AND that materially helps THEM —
        # capped. The help screen drops junk that would pad multi-player trades.
        give_pool = [
            pid for pid in me.surplus_ids
            if pid in values and values[pid].position in them.needs
            and contextual_value(_lp(pid), their_roster_lp, rules) > _HELP_EPSILON
        ][:_MATCH_POOL_CAP]
        # THEIR surplus at a position I need AND that materially helps ME — capped.
        get_pool = [
            pid for pid in them.surplus_ids
            if pid in values and values[pid].position in me.needs
            and contextual_value(_lp(pid), my_roster_lp, rules) > _HELP_EPSILON
        ][:_MATCH_POOL_CAP]
        if not give_pool or not get_pool:
            continue  # no two-sided surplus-for-need fit with this opponent

        opp_size = len(opp.roster)
        for give_n in range(1, _MAX_PER_SIDE + 1):
            for get_n in range(1, _MAX_PER_SIDE + 1):
                if get_n > len(get_pool) or give_n > len(give_pool):
                    continue
                if not _both_sides_fit(my_size, opp_size, give_n, get_n, roster_limit):
                    continue
                for give in itertools.combinations(give_pool, give_n):
                    for get in itertools.combinations(get_pool, get_n):
                        out.append(Candidate(tuple(give), tuple(get), opp.team_id))
    return out


def _lineup_roster(team: TeamState, values: dict[str, InSeasonValue]) -> list[LineupPlayer]:
    """A team's roster as LineupPlayers for the lineup / contextual primitives.
    Carries forward_ppg (for lineup_strength_ppg) and the #170 buy-low/ascending
    signal (for the depth clause §5a). Players without a computed value are skipped."""
    out = []
    for rp in team.roster:
        v = values.get(rp.canonical_player_id)
        if v is None:
            continue
        out.append(LineupPlayer(
            rp.canonical_player_id, rp.position, v.forward_value,
            forward_ppg=v.forward_ppg, rising=bool(v.buy_low),
        ))
    return out


def evaluate_edge_band(
    my_roster: list[LineupPlayer],
    their_roster: list[LineupPlayer],
    give_ids: tuple[str, ...] | list[str],
    get_ids: tuple[str, ...] | list[str],
    *,
    roster_limit: int = DEFAULT_ROSTER_LIMIT,
    rules: LineupRules | None = None,
) -> EdgeBand:
    """Score a candidate by the §6 lineup objective. Each side's gain is the change
    in its OPTIMAL STARTING LINEUP's points/week, computed ONCE on the RESULTING
    roster (after incoming + outgoing + forced drops) — not a per-player value sum.
    This kills the multi-player double-count and debits forced drops automatically.
    The §4 two-clause value rule decides "has value"; cond 3 keeps the edge; cond 4
    is the #168 relative overtake guard."""
    rules = rules or DEFAULT_LINEUP_RULES
    give_set, get_set = set(give_ids), set(get_ids)

    post = apply_trade(my_roster, their_roster, list(give_ids), list(get_ids))
    my_post = fit_to_limit(list(post.my_roster), roster_limit)
    their_post = fit_to_limit(list(post.their_roster), roster_limit)

    your_lineup_gain = round(lineup_strength_ppg(my_post, rules) - lineup_strength_ppg(my_roster, rules), 2)
    their_lineup_gain = round(lineup_strength_ppg(their_post, rules) - lineup_strength_ppg(their_roster, rules), 2)

    # Depth clause (§5a): an INCOMING bench piece (not in the resulting starting
    # lineup) carrying the #170 rising/buy-low signal — a stash, not flat churn.
    my_starters = {p.player_id for p in optimal_lineup(my_post, rules).starters}
    their_starters = {p.player_id for p in optimal_lineup(their_post, rules).starters}
    my_rising_bench = any(
        p.rising and p.player_id not in my_starters for p in my_post if p.player_id in get_set
    )
    their_rising_bench = any(
        p.rising and p.player_id not in their_starters for p in their_post if p.player_id in give_set
    )

    guard = overtake_guard(my_roster, their_roster, list(give_ids), list(get_ids), rules)  # cond 4 (#168)

    clears = (
        _has_value(your_lineup_gain, my_rising_bench)           # 1: value to you (§4)
        and _has_value(their_lineup_gain, their_rising_bench)   # 2: acceptable to them (§4, their roster)
        and your_lineup_gain > their_lineup_gain                # 3: you keep the edge
        and guard.passes                                        # 4: no overtake
    )
    return EdgeBand(
        your_lineup_gain=your_lineup_gain, their_lineup_gain=their_lineup_gain,
        my_strength=guard.my_strength, their_strength=guard.their_strength, clears=clears,
    )


def evaluate_candidates(
    state: LeagueState,
    values: dict[str, InSeasonValue],
    my_team_id: str,
    candidates: list[Candidate],
    *,
    roster_limit: int,
    max_results: int = MAX_PROPOSALS,
) -> list[tuple[Candidate, TradeAnalysis, EdgeBand]]:
    """Keep only candidates that CLEAR the four-condition edge band, rank by your
    edge (your_net) descending, cap. Pure + deterministic — the never-pad + cap
    guarantees live here; far fewer trades clear four conditions than one, so an
    empty result is the common, correct outcome (never loosen the gate to fill)."""
    my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
    if my_team is None:
        return []
    my_roster = _lineup_roster(my_team, values)
    opp_rosters = {
        t.team_id: _lineup_roster(t, values)
        for t in state.teams if t.team_id != my_team_id
    }

    scored: list[tuple[Candidate, TradeAnalysis, EdgeBand]] = []
    seen: set[tuple] = set()
    for cand in candidates:
        key = (cand.give_ids, cand.get_ids)
        if key in seen:
            continue
        seen.add(key)
        their_roster = opp_rosters.get(cand.counterparty_team_id)
        if their_roster is None:
            continue
        try:
            validate_trade(state, values, my_team_id, list(cand.give_ids), list(cand.get_ids))
            edge = evaluate_edge_band(
                my_roster, their_roster, cand.give_ids, cand.get_ids, roster_limit=roster_limit,
            )
            if not edge.clears:
                continue
            analysis = analyze_trade(
                state, values, my_team_id,
                list(cand.give_ids), list(cand.get_ids), roster_limit=roster_limit,
            )
        except Exception:
            continue  # unresolvable candidate — skip, never surface
        scored.append((cand, analysis, edge))

    scored.sort(key=lambda ce: ce[2].your_lineup_gain, reverse=True)
    return scored[:max_results]
