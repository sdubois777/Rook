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

# --- ASYMMETRIC EDGE-BAND GATE (trade_lineup_value_design.md + calibration) ---
# A trade is judged by the change in each STARTING LINEUP's projected points/week
# on the RESULTING roster (incoming + outgoing + forced drops), evaluated once.
# The gate is ASYMMETRIC + value-fairness (all thresholds MEASURED):
#   cond 1 — you improve:   Δlineup_me   >= _LINEUP_GAIN_THRESHOLD
#   cond 2 — they maintain: Δlineup_them >= -_MAINTAIN_TOL  (need only not get worse)
#   cond 3 — value-fair:    acquirer asset-value get/give ratio within [1/R, R]
#   cond 4 — overtake:      the #168 relative guard
# Requiring BOTH sides to GAIN >=5 (the old gate) demanded every trade be a major
# upgrade for both managers — rare, so almost nothing surfaced. Loosening cond 2 to
# "maintain" surfaces fair asymmetric trades; cond 3 (value ratio) kills the
# reverse-fleece that loosening would otherwise open (give a stud/startable bench
# for junk) while keeping deep-owner deals (Bijan ratio 1.30) + fair consolidations
# (Swift 3.92). Ratio, NOT absolute gap (gap doesn't separate the cases — measured).
_LINEUP_GAIN_THRESHOLD = 5.0   # cond 1: ppg starting-lineup gain the ACQUIRER must clear
_MAINTAIN_TOL = 0.5            # cond 2: the opponent need only not get WORSE on the field
_FAIRNESS_RATIO = 5.0         # cond 3: acquirer get/give asset-value ratio bound

# DEPRECATED (superseded by the lineup objective): the old contextual-value comfort
# epsilon. Kept only as the acceptability READ's marginal-band label.
_COMFORT_THRESHOLD = 2.0

# Targeted-enumeration bounds (slice 6, §6d/§6e). HARD CAP 3 players per side
# (Stephen: past 3-for-3 reeks of desperation / nobody accepts). The matched-pool
# cap bounds the combinatorial space — only need/surplus-matched pieces are ever
# combined, and at most this many per side.
_MAX_PER_SIDE = 3
_MATCH_POOL_CAP = 5
# Per-SHAPE generation cap. With the pools BROADENED to include startable players
# (not just bench surplus), the raw 1..3-per-side cross-product can blow up in
# value-clustered rosters. For each shape (give_n, get_n) we keep only the few most
# value-BALANCED packages — so every shape stays represented (a clean 1-for-1 starter
# swap isn't crowded out by the many near-zero-imbalance 3-for-3s a bigger package
# trivially hits) while the count stays bounded. The gate still judges every survivor.
_PER_SHAPE_CAP = 2
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


def _value_fair(get_val: float, give_val: float) -> bool:
    """cond 3 — anti-reverse-fleece. The acquirer's ASSET-value get/give ratio
    (Σforward_value) must be within [1/R, R]; rejects trades where one side gives
    up more than R× the value it receives (the McLaurin-for-junk class, ratio 16.7)
    while keeping deep-owner deals (Bijan, 1.30) and fair consolidations (Swift,
    3.92). Ratio, not absolute gap (measured: gap doesn't separate the cases).
    Guards BOTH directions: give-nothing-for-something is as unfair as the reverse."""
    if give_val <= 0:
        return get_val <= 0          # giving no real value for something → unfair
    return (1.0 / _FAIRNESS_RATIO) <= (get_val / give_val) <= _FAIRNESS_RATIO


# Acceptability verdicts (the analyzer's opponent-side READ). NOT a gate — any
# trade is analyzed and labeled honestly — but the LABEL reads acceptance from the
# SAME rule the proposals gate (#174) uses for the opponent's side, so the two
# screens can never disagree on the same trade: a rational manager accepts a trade
# that MAINTAINS their lineup (Δlineup_them >= -_MAINTAIN_TOL) and is VALUE-FAIR
# (acquirer ratio within [1/R, R]); rejects one that drops their lineup or fleeces
# them. Read off _MAINTAIN_TOL + _FAIRNESS_RATIO (via _value_fair) — one source of
# truth with the gate, so they can't drift.
ACCEPT_LIKELY = "likely_accept"      # maintain + fair AND they meaningfully improve (> _MAINTAIN_TOL)
ACCEPT_MARGINAL = "marginal"         # maintain + fair but ~neutral (a lateral they may not jump at)
ACCEPT_REJECT = "likely_reject"      # lineup drops (< -_MAINTAIN_TOL) OR not value-fair (fleece)


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


def _acceptability_verdict(their_lineup_gain: float, fair: bool) -> str:
    """The opponent-side acceptance label, read from the SAME rule the gate (#174)
    applies: accept iff their lineup MAINTAINS (>= -_MAINTAIN_TOL) AND the trade is
    value-fair; otherwise reject. The maintain band ([-_MAINTAIN_TOL, _MAINTAIN_TOL])
    is the 'marginal' lateral; a clear lineup gain above it is likely_accept."""
    if not fair or their_lineup_gain < -_MAINTAIN_TOL:
        return ACCEPT_REJECT
    if their_lineup_gain > _MAINTAIN_TOL:
        return ACCEPT_LIKELY
    return ACCEPT_MARGINAL


def _acceptability_why(
    values: dict[str, InSeasonValue],
    their_roster: list[LineupPlayer],
    give_ids: tuple[str, ...] | list[str],
    verdict: str,
    fair: bool,
    overtake_flag: bool,
    hedged: bool,
) -> str:
    """A one-line WHY grounded in THEIR side, reflecting the REAL accept/reject
    reason (maintain + fair vs lineup-drop vs fleece)."""
    def _cv(pid: str) -> float:
        v = values[pid]
        return contextual_value(LineupPlayer(pid, v.position, v.forward_value), their_roster)

    incoming = [g for g in give_ids if g in values]
    best = max(incoming, key=_cv) if incoming else None
    name = values[best].name if best else "what you're sending"
    pos = values[best].position if best else "that spot"

    if verdict == ACCEPT_LIKELY:
        why = f"{name} improves their {pos} and it's fair value — they'd likely accept"
    elif verdict == ACCEPT_MARGINAL:
        why = f"maintains their lineup at fair value — a lateral they may not jump at"
    elif not fair:
        why = "they'd be giving up far more value than they get"
    else:
        why = "this drops their starting lineup"
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
    # Value-fairness read off the SAME helper + constants the gate uses (the
    # acquirer's asset-value get/give ratio) — one source of truth, can't drift.
    my_by = {p.player_id: p for p in my_roster}
    their_by = {p.player_id: p for p in their_roster}
    give_val = sum(my_by[g].forward_value for g in give_ids if g in my_by)
    get_val = sum(their_by[g].forward_value for g in get_ids if g in their_by)
    fair = _value_fair(get_val, give_val)
    verdict = _acceptability_verdict(edge.their_lineup_gain, fair)
    why = _acceptability_why(values, their_roster, give_ids, verdict, fair, overtake_flag, hedged)
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
        # GIVE-pool: ANY of my players (surplus OR a startable piece I can spare) at
        # a position THEY need that materially helps THEM — so I can pay fair value
        # for a starter, not only dump bench. Top-valued, capped.
        give_pool = sorted(
            (rp.canonical_player_id for rp in my_team.roster
             if rp.canonical_player_id in values
             and values[rp.canonical_player_id].position in them.needs
             and contextual_value(_lp(rp.canonical_player_id), their_roster_lp, rules) > _HELP_EPSILON),
            key=lambda pid: -values[pid].forward_value,
        )[:_MATCH_POOL_CAP]
        # GET-pool: ANY of their players at a position I need that materially improves
        # MY lineup — STARTABLE players, not just their bench surplus (the funnel fix).
        get_pool = sorted(
            (rp.canonical_player_id for rp in opp.roster
             if rp.canonical_player_id in values
             and values[rp.canonical_player_id].position in me.needs
             and contextual_value(_lp(rp.canonical_player_id), my_roster_lp, rules) > _HELP_EPSILON),
            key=lambda pid: -values[pid].forward_value,
        )[:_MATCH_POOL_CAP]
        if not give_pool or not get_pool:
            continue  # no two-sided fit with this opponent

        opp_size = len(opp.roster)
        for give_n in range(1, _MAX_PER_SIDE + 1):
            for get_n in range(1, _MAX_PER_SIDE + 1):
                if get_n > len(get_pool) or give_n > len(give_pool):
                    continue
                if not _both_sides_fit(my_size, opp_size, give_n, get_n, roster_limit):
                    continue
                shape: list[tuple[float, float, tuple[str, ...], tuple[str, ...]]] = []
                for give in itertools.combinations(give_pool, give_n):
                    give_val = sum(values[g].forward_value for g in give)
                    for get in itertools.combinations(get_pool, get_n):
                        # VALUE-FAIRNESS pre-filter (the gate's cond-3 rule): only
                        # build packages of comparable asset value, so a starter is
                        # paid for fairly — and the candidate space stays bounded (no
                        # give-junk-for-their-stud fleeces are even generated).
                        get_val = sum(values[g].forward_value for g in get)
                        if not _value_fair(get_val, give_val):
                            continue
                        # Rank within the shape by value-balance (then richer target).
                        shape.append((abs(get_val - give_val), -get_val, give, get))
                shape.sort(key=lambda s: (s[0], s[1], s[2], s[3]))
                for _, _, give, get in shape[:_PER_SHAPE_CAP]:
                    out.append(Candidate(give, get, opp.team_id))
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
    """Score a candidate by the ASYMMETRIC lineup-objective gate. Each side's gain
    is the change in its OPTIMAL STARTING LINEUP's points/week, computed ONCE on the
    RESULTING roster (after incoming + outgoing + forced drops) — not a per-player
    value sum. The four conditions: (1) the acquirer's lineup improves >=threshold,
    (2) the opponent's lineup at least MAINTAINS, (3) the trade is value-fair (asset
    ratio within [1/R, R] — anti-reverse-fleece), (4) the #168 overtake guard."""
    rules = rules or DEFAULT_LINEUP_RULES

    post = apply_trade(my_roster, their_roster, list(give_ids), list(get_ids))
    my_post = fit_to_limit(list(post.my_roster), roster_limit)
    their_post = fit_to_limit(list(post.their_roster), roster_limit)

    your_lineup_gain = round(lineup_strength_ppg(my_post, rules) - lineup_strength_ppg(my_roster, rules), 2)
    their_lineup_gain = round(lineup_strength_ppg(their_post, rules) - lineup_strength_ppg(their_roster, rules), 2)

    # cond 3 asset value (Σforward_value) of what the ACQUIRER gives vs gets.
    my_by = {p.player_id: p for p in my_roster}
    their_by = {p.player_id: p for p in their_roster}
    give_val = sum(my_by[g].forward_value for g in give_ids if g in my_by)
    get_val = sum(their_by[g].forward_value for g in get_ids if g in their_by)

    guard = overtake_guard(my_roster, their_roster, list(give_ids), list(get_ids), rules)  # cond 4 (#168)

    clears = (
        your_lineup_gain >= _LINEUP_GAIN_THRESHOLD     # 1: you improve (acquirer)
        and their_lineup_gain >= -_MAINTAIN_TOL        # 2: they maintain (not worse on the field)
        and _value_fair(get_val, give_val)             # 3: value-fair (no reverse-fleece)
        and guard.passes                               # 4: no overtake
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
