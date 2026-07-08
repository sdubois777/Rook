"""
Tier-1 start/sit REASONING — per-starter opponent matchup grade, injury-aware
optimal lineup (Out/IR excluded, Q/D flagged), and FOUNDED bench-swap surfacing.
Pure/deterministic — ZERO-metered (no Sonnet/credit).

Honest scope: there is NO user-set lineup, only the computed optimal. So this is
"here's your best AVAILABLE lineup + the matchup reason per starter, plus any bench
player who's as good with a softer draw" — reasoning, not "start X / bench Y".

Coverage = WR/RB/TE (+ a FLEX filled by one of those) — the positions
compute_def_grades grades. QB/K/DEF get no matchup tag (QB grade not built; DEF has
its own tilt). Weather (T2) and opponent-injury/backup (T3) are out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backend.agents.schedule import lookup_def_grade
from backend.services.trade.lineup import DEFAULT_LINEUP_RULES, LineupRules, _slot_pos, optimal_lineup

COVERED_POSITIONS = ("WR", "RB", "TE")     # def-grade coverage
UNAVAILABLE_STATUS = ("O", "IR")            # can't play → excluded from the optimal lineup
FLAG_STATUS = ("Q", "D")                    # may play → flagged, NOT excluded, NOT down-weighted

# A bench player is a real swap candidate only if he's within this many ppg of the
# starter he'd replace (competitive on value — "not clearly worse") AND draws a
# materially softer matchup. Both required, else no swap (silence is correct).
SWAP_PPG_MARGIN = 2.0

# grade → tier (lower = softer/better for the offense). None (bye/ungraded) sits worst.
_GRADE_TIER = {"favorable": 0, "neutral": 1, "tough": 2}


@dataclass(frozen=True)
class StarterMatchup:
    player_id: str
    name: str
    position: str
    slot: str
    nfl_team: Optional[str]
    opponent: Optional[str]        # None → bye / no scheduled game (BYE/na, no grade)
    grade: Optional[str]           # favorable | neutral | tough, or None (bye)
    def_rank: Optional[int]
    injury_flag: Optional[str]     # "Q" | "D" or None — a monitor flag, not a downgrade
    forward_ppg: float


@dataclass(frozen=True)
class BenchSwap:
    position: str
    slot: str
    starter_name: str
    starter_grade: Optional[str]
    starter_ppg: float
    bench_name: str
    bench_opponent: Optional[str]
    bench_grade: Optional[str]
    bench_ppg: float
    reason: str


@dataclass(frozen=True)
class Replacement:
    out_name: str
    out_status: str                # "O" | "IR"
    position: str
    slot: str
    in_name: Optional[str]         # who fills the slot in the available optimal (None if unfilled)


@dataclass(frozen=True)
class StartSit:
    starters: tuple[StarterMatchup, ...] = ()
    swaps: tuple[BenchSwap, ...] = ()
    replacements: tuple[Replacement, ...] = ()
    covered_positions: tuple[str, ...] = COVERED_POSITIONS


def _grade_for(def_grades, opponent: Optional[str], position: str):
    """(grade, rank) for opponent vs position, or (None, None) when there's no
    opponent (bye) — never fabricate a grade for a player who isn't playing."""
    if not opponent:
        return None, None
    grade = lookup_def_grade(def_grades, opponent, position)
    rank = None
    if def_grades is not None and not def_grades.empty:
        m = def_grades[(def_grades["defense_team"] == opponent) & (def_grades["position"] == position)]
        if not m.empty:
            rank = int(m.iloc[0]["rank"])
    return grade, rank


def _softer(bench_grade: Optional[str], starter_grade: Optional[str]) -> bool:
    """A MATERIALLY softer draw: at least one clear grade tier better (favorable over
    neutral/tough, or neutral over tough). Bye/ungraded on either side → not material."""
    if bench_grade is None or starter_grade is None:
        return False
    return _GRADE_TIER[bench_grade] <= _GRADE_TIER[starter_grade] - 1


def build_start_sit(
    team,                                    # TeamState (roster carries injury_status)
    values: dict,                            # {pid: InSeasonValue}
    def_grades,                              # as-of-week compute_def_grades frame
    nfl_opponent_by_team: dict[str, str],    # {nfl_team_abbr: opponent this week}
    rules: Optional[LineupRules] = None,
) -> StartSit:
    """Build the Tier-1 panel for one team: injury-aware optimal lineup, per-starter
    matchup grade (covered positions), Out/IR replacements, and founded bench swaps."""
    rules = rules or DEFAULT_LINEUP_RULES
    from backend.services.trade.trade_proposals import _lineup_roster

    inj = {rp.canonical_player_id: rp.injury_status for rp in team.roster}
    nfl_team = {rp.canonical_player_id: rp.nfl_team for rp in team.roster}
    name_of = {rp.canonical_player_id: rp.name for rp in team.roster}

    full_lps = _lineup_roster(team, values)
    # Out/IR can't be in your best AVAILABLE lineup this week.
    available = [lp for lp in full_lps if inj.get(lp.player_id) not in UNAVAILABLE_STATUS]

    full_ol = optimal_lineup(full_lps, rules)
    avail_ol = optimal_lineup(available, rules)
    full_starter_ids = {p.player_id for p in full_ol.starters}
    avail_starter_ids = {p.player_id for p in avail_ol.starters}

    # --- Replacements: an Out/IR player who WOULD start (full optimal) is excluded;
    #     name who NEWLY starts in his place (a player in the available optimal but not
    #     the full one — the honest "fills in", not a starter who merely shifted slots).
    promoted = [p for p in avail_ol.starters if p.player_id not in full_starter_ids]
    replacements: list[Replacement] = []
    for label, pid in full_ol.slots:
        if pid is None or inj.get(pid) not in UNAVAILABLE_STATUS:
            continue
        pos = values[pid].position if pid in values else _slot_pos(label)
        # prefer a same-position promotion, else any newly-starting player.
        fill = next((p for p in promoted if p.player_id in values and values[p.player_id].position == pos), None)
        fill = fill or (promoted[0] if promoted else None)
        if fill is not None:
            promoted.remove(fill)
        replacements.append(Replacement(
            out_name=name_of.get(pid, pid), out_status=inj[pid], position=pos, slot=label,
            in_name=name_of.get(fill.player_id) if fill else None,
        ))

    # --- Per-starter matchup (covered positions only) ---
    by_pid = {lp.player_id: lp for lp in available}
    starters: list[StarterMatchup] = []
    for label, pid in avail_ol.slots:
        if pid is None or pid not in values:
            continue
        pos = values[pid].position
        if pos not in COVERED_POSITIONS:
            continue                          # QB/K/DEF → no matchup tag
        team_abbr = nfl_team.get(pid)
        opp = nfl_opponent_by_team.get((team_abbr or "").upper()) if team_abbr else None
        grade, rank = _grade_for(def_grades, opp, pos)
        flag = inj.get(pid) if inj.get(pid) in FLAG_STATUS else None
        starters.append(StarterMatchup(
            player_id=pid, name=name_of.get(pid, pid), position=pos, slot=label,
            nfl_team=team_abbr, opponent=opp, grade=grade, def_rank=rank,
            injury_flag=flag, forward_ppg=round(by_pid[pid].forward_ppg, 2) if pid in by_pid else 0.0,
        ))

    # --- Founded bench swaps: per covered position, at most one, both conditions ---
    swaps: list[BenchSwap] = []
    for pos in COVERED_POSITIONS:
        pos_starters = [s for s in starters if s.position == pos]
        if not pos_starters:
            continue
        weakest = min(pos_starters, key=lambda s: s.forward_ppg)
        bench = [lp for lp in available
                 if lp.player_id not in avail_starter_ids
                 and lp.player_id in values and values[lp.player_id].position == pos]
        best: Optional[BenchSwap] = None
        for b in bench:
            if b.forward_ppg < weakest.forward_ppg - SWAP_PPG_MARGIN:
                continue                      # clearly worse on value → not a swap
            b_team = nfl_team.get(b.player_id)
            b_opp = nfl_opponent_by_team.get((b_team or "").upper()) if b_team else None
            b_grade, _ = _grade_for(def_grades, b_opp, pos)
            if not _softer(b_grade, weakest.grade):
                continue                      # not a materially softer draw → no swap
            cand = BenchSwap(
                position=pos, slot=weakest.slot,
                starter_name=weakest.name, starter_grade=weakest.grade, starter_ppg=weakest.forward_ppg,
                bench_name=name_of.get(b.player_id, b.player_id), bench_opponent=b_opp,
                bench_grade=b_grade, bench_ppg=round(b.forward_ppg, 2),
                reason=(f"{name_of.get(b.player_id, '')} is within {SWAP_PPG_MARGIN} ppg and draws a "
                        f"softer matchup ({b_grade} vs {b_opp}) than {weakest.name} "
                        f"({weakest.grade} vs {weakest.opponent})"),
            )
            # prefer the softest matchup, then the higher-value bench option.
            if best is None or (_GRADE_TIER.get(b_grade, 3), -b.forward_ppg) < (_GRADE_TIER.get(best.bench_grade, 3), -best.bench_ppg):
                best = cand
        if best is not None:
            swaps.append(best)

    return StartSit(
        starters=tuple(starters), swaps=tuple(swaps), replacements=tuple(replacements),
    )


def available_lineup_roster(team, values, rules: Optional[LineupRules] = None):
    """The team's roster as LineupPlayers with Out/IR EXCLUDED — the injury-aware
    input to optimal_lineup / lineup_strength_ppg so an unavailable player is never in
    the 'best lineup'. Shared by _scout so the H2H margin/grid match the panel."""
    from backend.services.trade.trade_proposals import _lineup_roster
    inj = {rp.canonical_player_id: rp.injury_status for rp in team.roster}
    return [lp for lp in _lineup_roster(team, values)
            if inj.get(lp.player_id) not in UNAVAILABLE_STATUS]
