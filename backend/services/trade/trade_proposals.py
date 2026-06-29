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

from dataclasses import dataclass

from backend.services.trade.contextual import contextual_value
from backend.services.trade.league_state import LeagueState, TeamState
from backend.services.trade.lineup import LineupPlayer
from backend.services.trade.overtake import overtake_guard
from backend.services.trade.trade_analysis import (
    TradeAnalysis,
    analyze_trade,
    validate_trade,
)
from backend.services.trade.value_engine import InSeasonValue

MAX_PROPOSALS = 5

# Condition 2 (Fork 3a): the opponent must improve COMFORTABLY, not by a
# rounding-error margin they'd haggle over. Named tunable constant (contextual-
# value points; the 0-100 forward_value scale). v1 default — Stephen calibrates.
_COMFORT_THRESHOLD = 3.0


@dataclass(frozen=True)
class Candidate:
    give_ids: tuple[str, ...]
    get_ids: tuple[str, ...]
    counterparty_team_id: str


@dataclass(frozen=True)
class EdgeBand:
    """The §3 edge-band scoring of a candidate (all contextual). Surfaced numbers
    so the analyzer/UI can show why a trade cleared (or didn't)."""
    your_net: float
    their_net: float
    my_strength: float        # post-trade starting strength (mine)
    their_strength: float     # post-trade starting strength (theirs)
    clears: bool


# Acceptability verdicts (the analyzer's opponent-side READ, §5/§6c). NOT a gate:
# any trade is evaluated and reported honestly — "great for you, they'd reject it"
# is surfaced as a rejection, never rounded up to a win.
ACCEPT_LIKELY = "likely_accept"      # their_net > _COMFORT_THRESHOLD
ACCEPT_MARGINAL = "marginal"         # 0 < their_net <= _COMFORT_THRESHOLD
ACCEPT_REJECT = "likely_reject"      # their_net <= 0


@dataclass(frozen=True)
class Acceptability:
    """Would the OTHER side likely accept the trade the user built? A read derived
    from the slice-4 edge band evaluated from the counterparty's perspective —
    their_net (their contextual gain) drives the verdict, the overtake guard adds
    the 'helps them more on the field' flag, and ``why`` is grounded in their
    roster. It is a READ, not a filter (§6c)."""
    verdict: str
    their_net: float
    overtake_flag: bool       # the trade would make THEIR lineup overtake yours
    hedged: bool              # opponent-side data is limited/insufficient → soft read
    why: str


def _verdict_for(their_net: float) -> str:
    if their_net > _COMFORT_THRESHOLD:
        return ACCEPT_LIKELY
    if their_net <= 0:
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
        edge = evaluate_edge_band(my_roster, their_roster, give_ids, get_ids)
    except Exception:
        return Acceptability(ACCEPT_REJECT, 0.0, False, hedged,
                             "could not evaluate the other side of this trade")

    overtake_flag = edge.my_strength < edge.their_strength   # guard failed → they overtake
    verdict = _verdict_for(edge.their_net)
    why = _acceptability_why(values, their_roster, give_ids, verdict, overtake_flag, hedged)
    return Acceptability(verdict, edge.their_net, overtake_flag, hedged, why)


def enumerate_candidates(state: LeagueState, my_team_id: str) -> list[Candidate]:
    """Deterministic fallback search: every 1-for-1 swap between my roster and
    each opponent's roster. Plausibility (need/surplus targeting) is the LLM's
    job; the edge-band gate is what actually decides what surfaces, so an
    exhaustive 1-for-1 enumeration is a safe, non-random fallback."""
    my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
    if my_team is None:
        return []
    out: list[Candidate] = []
    for opp in state.teams:
        if opp.team_id == my_team_id:
            continue
        for mp in my_team.roster:
            for op in opp.roster:
                out.append(Candidate(
                    (mp.canonical_player_id,), (op.canonical_player_id,), opp.team_id,
                ))
    return out


def _lineup_roster(team: TeamState, values: dict[str, InSeasonValue]) -> list[LineupPlayer]:
    """A team's roster as LineupPlayers (position + forward_value) for the lineup
    / contextual primitives. Players without a computed value are skipped."""
    return [
        LineupPlayer(rp.canonical_player_id, rp.position, values[rp.canonical_player_id].forward_value)
        for rp in team.roster
        if rp.canonical_player_id in values
    ]


def evaluate_edge_band(
    my_roster: list[LineupPlayer],
    their_roster: list[LineupPlayer],
    give_ids: tuple[str, ...] | list[str],
    get_ids: tuple[str, ...] | list[str],
) -> EdgeBand:
    """Score a candidate against the §3 edge band. PERSPECTIVE is the crux: each
    player is valued against the roster EVALUATING the trade — a player CURRENTLY
    ON a roster is valued against that roster WITHOUT him (what that side loses),
    an INCOMING player against the roster as-is (what that side gains)."""
    my_by = {p.player_id: p for p in my_roster}
    their_by = {p.player_id: p for p in their_roster}
    give = [my_by[g] for g in give_ids]      # on MY roster, going to them
    get = [their_by[g] for g in get_ids]     # on THEIR roster, coming to me

    def _without(roster, pid):
        return [p for p in roster if p.player_id != pid]

    # Your perspective (against MY roster).
    your_get_ctx = sum(contextual_value(x, my_roster) for x in get)                       # incoming
    your_give_ctx = sum(contextual_value(g, _without(my_roster, g.player_id)) for g in give)  # what I lose
    your_net = round(your_get_ctx - your_give_ctx, 1)

    # Their perspective (against THEIR roster) — NOT the negation of your_net.
    their_get_ctx = sum(contextual_value(g, their_roster) for g in give)                  # my players, incoming to them
    their_give_ctx = sum(contextual_value(x, _without(their_roster, x.player_id)) for x in get)  # what they lose
    their_net = round(their_get_ctx - their_give_ctx, 1)

    guard = overtake_guard(my_roster, their_roster, list(give_ids), list(get_ids))  # condition 4

    clears = (
        your_net > 0                          # 1: you improve
        and their_net > _COMFORT_THRESHOLD    # 2: they improve comfortably
        and your_net > their_net              # 3: you keep the edge
        and guard.passes                      # 4: you don't fall behind on the field
    )
    return EdgeBand(
        your_net=your_net, their_net=their_net,
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
            edge = evaluate_edge_band(my_roster, their_roster, cand.give_ids, cand.get_ids)
            if not edge.clears:
                continue
            analysis = analyze_trade(
                state, values, my_team_id,
                list(cand.give_ids), list(cand.get_ids), roster_limit=roster_limit,
            )
        except Exception:
            continue  # unresolvable candidate — skip, never surface
        scored.append((cand, analysis, edge))

    scored.sort(key=lambda ce: ce[2].your_net, reverse=True)
    return scored[:max_results]
