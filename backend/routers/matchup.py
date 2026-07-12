"""
Matchup router — GET /api/matchup/league.

H2H league-opponent SCOUTING for the acting team: this week's synthesized
opponent, both projected optimal-lineup ppw + the margin, a positional battle grid
(by optimal-lineup slot), the 12-team season-long strength ladder, and a
needs/surplus leverage readout. It is a ZERO-METERED-COST surface — every value is
a pure/deterministic primitive over the SAME evaluate_league bundle /trade/league
uses (so numbers match across pages). No Sonnet, no metered agent, no credit path.

The only route to a metered call is the frontend's explicit handoff to the trade
Build tab (?opponent=<id>) — this endpoint never runs the finder or analyzer.

Demo-only: reuses the existing TRADE_DEMO_MODE seam (404 when off) — no new flag.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.core.dependencies import get_current_user, get_db
from backend.services.matchup.scouting import (
    GRID_POSITIONS,
    confidence_summary,
    leverage_readout,
    opponent_of,
    positional_slot_ppg,
    synthesize_week_matchups,
    win_prob_band,
)
from backend.services.matchup.startsit import available_lineup_roster, build_start_sit
from backend.services.trade.lineup import lineup_rules_from_slots, lineup_strength_ppg, roster_strength
from backend.services.trade.trade_demo_source import trade_demo_enabled, trade_demo_enforce_gates
from backend.services.trade.trade_proposals import _lineup_roster, analyze_roster
from backend.services.trade.value_engine import replacement_ppg_by_position

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/matchup", tags=["matchup"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class LadderRowOut(BaseModel):
    team_id: str
    team_name: str
    is_me: bool
    strength: float          # optimal starting-lineup strength (0-100 basis, same as trade)
    ppw: float               # projected optimal-lineup points/week


class PairOut(BaseModel):
    home_team_id: str
    away_team_id: str
    home_team_name: str
    away_team_name: str


class GridRowOut(BaseModel):
    position: str
    mine: float              # my startable ppw at this position (optimal-lineup slot)
    theirs: float            # opponent's startable ppw at this position


class StarterMatchupOut(BaseModel):
    """One covered starter (WR/RB/TE/FLEX) with its as-of-week opponent matchup grade
    and an optional injury monitor flag. grade None = bye / no scheduled game."""
    name: str
    position: str
    slot: str
    nfl_team: Optional[str] = None
    opponent: Optional[str] = None       # None → BYE/na (no fabricated grade)
    grade: Optional[str] = None          # favorable | neutral | tough
    injury_flag: Optional[str] = None    # "Q" | "D" — monitor, not a downgrade
    forward_ppg: float
    unfillable: bool = False             # no available player for this slot (bye/Out/IR)
    unfillable_reason: Optional[str] = None   # e.g. "Kareem Hunt is on bye — no available RB"


class BenchSwapOut(BaseModel):
    """A FOUNDED bench-swap suggestion: competitive on value AND a materially softer
    draw. A suggestion, not a directive."""
    position: str
    starter_name: str
    starter_grade: Optional[str] = None
    bench_name: str
    bench_opponent: Optional[str] = None
    bench_grade: Optional[str] = None
    reason: str


class ReplacementOut(BaseModel):
    """An Out/IR starter excluded from the optimal lineup + who deterministically
    fills the slot. Honest reaction, not a value claim."""
    out_name: str
    out_status: str                      # "O" | "IR"
    position: str
    in_name: Optional[str] = None


class StartSitOut(BaseModel):
    """Tier-1 matchup reasoning: per-starter matchup grades, Out/IR replacements, and
    founded bench swaps. Reasoning, not 'start X / bench Y'. Zero-metered."""
    starters: list[StarterMatchupOut]
    swaps: list[BenchSwapOut]
    replacements: list[ReplacementOut]
    covered_positions: list[str]


class ScoutOut(BaseModel):
    """The acting team's H2H scouting vs its week opponent. All non-metered."""
    opponent_team_id: str
    opponent_team_name: str
    my_ppw: float
    opp_ppw: float
    margin: float                        # my_ppw - opp_ppw (the honest headline)
    win_prob_band: str                   # APPROXIMATE qualitative band from margin
    band_is_approximate: bool = True     # always true — no per-player variance exists
    confidence_note: str                 # coarsest confidence across both lineups
    grid: list[GridRowOut]
    # Leverage readout (the free scouting fact that motivates the paid trade-find).
    # Surplus positions are VALUE-GATED (real tradeable depth, not bench headcount)
    # and RECONCILED (a position never appears in both needs and surplus).
    my_needs: list[str]
    my_surplus_positions: list[str]
    opp_needs: list[str]
    opp_surplus_positions: list[str]
    their_surplus_my_needs: list[str]    # their real depth ∩ your needs
    my_surplus_their_needs: list[str]    # your real depth ∩ their needs
    is_reciprocal_fit: bool = False      # BOTH directions non-empty → a real mirror
    start_sit: Optional[StartSitOut] = None   # Tier-1 matchup reasoning (acting team)


class MatchupLeagueResponse(BaseModel):
    season: int
    week: int
    my_team_id: str
    my_team_name: str
    teams: list[LadderRowOut]            # season-long strength ladder (all 12)
    matchups: list[PairOut]              # every team's opponent this week
    scout: Optional[ScoutOut]           # None only if the acting team is byed
    demo_mode: bool
    enforced: bool


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@router.get("/league", response_model=MatchupLeagueResponse)
async def league(
    my_team_id: Optional[str] = None,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Read-only H2H scouting for the acting team (defaults to the is_me team; the
    'Acting as' switcher passes ``my_team_id``). Demo-only: 404s with TRADE_DEMO_MODE
    off (no real-league exposure — the real provider is trade slice 6)."""
    # Un-gated: demo ON serves the seeded demo league; demo OFF serves the user's real
    # synced league via the same seam (UndraftedLeagueError → 409 before the value path
    # when undrafted; 404 when none is synced).
    demo = trade_demo_enabled()

    # SAME seam + SAME evaluate_league (incl. the slice-4 DST tilt) as /trade/league,
    # so every number is consistent across pages. This path touches ONLY pure
    # primitives — no agent, no credit deduction.
    from backend.routers.trade import load_league_for_analysis
    # (Fix: the trade arc widened this to a 4-tuple — the rigid 3-value unpack
    # crashed this endpoint with ValueError. Star-unpack tolerates the shape;
    # behavior otherwise unchanged — only state + values are consumed here.)
    state, values, *_extra = await load_league_for_analysis(db, user, demo)

    # The league's real starting-slot shape drives the optimal lineup — read the
    # league's roster_slots through the canonical bridge (the demo seeds a 2-WR config;
    # a real per-league read from UserLeague.roster_slots is a flagged follow-up). No
    # slots (real league, not yet wired) → lineup_rules_from_slots(None) = DEFAULT
    # (unchanged behavior); loud-warn so the fallback is visible, never silent.
    if not state.roster_slots:
        logger.warning("matchup: league %s has no roster_slots — falling back to DEFAULT lineup rules "
                       "(real per-league slot reading is a follow-up)", getattr(state, "season", "?"))
    rules = lineup_rules_from_slots(state.roster_slots)
    replacement = replacement_ppg_by_position(values)

    # Resolve the acting team.
    acting = None
    if my_team_id:
        acting = next((t for t in state.teams if t.team_id == my_team_id), None)
        if acting is None:
            raise HTTPException(status_code=400, detail=f"team {my_team_id!r} not in league")
    # NEVER positional — bound is_me team or an explicit switcher pick only. A no-match
    # fails loud (the user's identity didn't bind a team); it must not scout a stranger's.
    acting = acting or state.my_team
    if acting is None:
        raise HTTPException(
            status_code=409,
            detail="Couldn't identify your team in this league — re-sync the league, or "
                   "pass my_team_id to act as a specific team.",
        )

    # --- season-long strength ladder (all 12, same value basis) ---
    def _ppw(team):
        return lineup_strength_ppg(_lineup_roster(team, values), rules, replacement)

    ladder = sorted(
        (LadderRowOut(
            team_id=t.team_id, team_name=t.team_name, is_me=t.is_me,
            strength=roster_strength(_lineup_roster(t, values), rules),
            ppw=_ppw(t),
        ) for t in state.teams),
        key=lambda r: -r.strength,
    )

    # --- synthesized week schedule (deterministic; permanent WeeklyMatchup shape) ---
    matchups = synthesize_week_matchups([t.team_id for t in state.teams], state.week)
    name_by_id = {t.team_id: t.team_name for t in state.teams}
    pairs = [PairOut(
        home_team_id=m.home_team_id, away_team_id=m.away_team_id,
        home_team_name=name_by_id.get(m.home_team_id, m.home_team_id),
        away_team_name=name_by_id.get(m.away_team_id, m.away_team_id),
    ) for m in matchups]

    # --- Tier-1 start/sit inputs: NFL opponent per team + as-of-week def grades ---
    # Both pure/cached; run off-thread so the (first-call) PBP build doesn't block the
    # event loop. Point-in-time (weeks 1..W-1) — no look-ahead.
    import asyncio

    from backend.integrations.nfl_data import fetch_schedules
    from backend.services.kdef_matchup import opponent_by_team
    from backend.services.matchup.def_grades import as_of_week_def_grades

    sched = await asyncio.to_thread(fetch_schedules, state.season)
    nfl_opp = {str(k).upper(): v for k, v in opponent_by_team(sched, state.week).items()}
    def_grades = await asyncio.to_thread(as_of_week_def_grades, state.season, state.week)

    # --- scout the acting team's opponent ---
    scout = None
    opp_id = opponent_of(matchups, acting.team_id)
    if opp_id is not None:
        opp = next((t for t in state.teams if t.team_id == opp_id), None)
        if opp is not None:
            scout = _scout(acting, opp, values, rules, replacement, def_grades, nfl_opp)

    return MatchupLeagueResponse(
        season=state.season, week=state.week,
        my_team_id=acting.team_id, my_team_name=acting.team_name,
        teams=ladder, matchups=pairs, scout=scout,
        demo_mode=demo, enforced=trade_demo_enforce_gates(),
    )


def _scout(acting, opp, values, rules, replacement, def_grades=None, nfl_opp=None) -> ScoutOut:
    # Unavailable-aware: an Out/IR OR bye player can't be in your best lineup this week,
    # so both teams' optimal lineups (margin/grid/start-sit) exclude them consistently.
    my_roster = available_lineup_roster(acting, values, rules, nfl_opp or {})
    opp_roster = available_lineup_roster(opp, values, rules, nfl_opp or {})

    # An unfillable required slot (no available player) contributes 0 — the honest
    # "you have nobody" number (with a waiver pointer on the panel), not a phantom
    # replacement floor — so my_ppw reconciles with the panel's seated-starter sum.
    my_ppw = lineup_strength_ppg(my_roster, rules, None)
    opp_ppw = lineup_strength_ppg(opp_roster, rules, None)
    margin = round(my_ppw - opp_ppw, 2)

    # Coarse confidence across BOTH optimal lineups → widens the toss-up band only.
    from backend.services.trade.lineup import optimal_lineup
    conf_vals = [
        values[p.player_id].confidence
        for roster in (my_roster, opp_roster)
        for p in optimal_lineup(roster, rules).starters
        if p.player_id in values
    ]
    conf_note, low_conf = confidence_summary(conf_vals)
    band = win_prob_band(margin, low_confidence=low_conf)

    my_grid = positional_slot_ppg(my_roster, rules, None)
    opp_grid = positional_slot_ppg(opp_roster, rules, None)
    grid = [GridRowOut(position=pos, mine=my_grid.get(pos, 0.0), theirs=opp_grid.get(pos, 0.0))
            for pos in GRID_POSITIONS]

    # Leverage — VALUE-GATED surplus (real tradeable depth, not bench headcount),
    # RECONCILED so a position never lands in both need and surplus, and a
    # RECIPROCAL mirror flag. analyze_roster (shared) is unchanged; the
    # value-awareness lives on the Matchup surface only.
    my_an = analyze_roster(acting, values, rules)
    opp_an = analyze_roster(opp, values, rules)
    lev = leverage_readout(
        my_an.needs, my_an.surplus_ids, opp_an.needs, opp_an.surplus_ids,
        values, replacement,
    )

    # Tier-1 start/sit — per-starter matchup grade, Out/IR replacements, founded swaps.
    start_sit_out = _build_start_sit_out(acting, values, def_grades, nfl_opp or {}, rules)

    return ScoutOut(
        opponent_team_id=opp.team_id, opponent_team_name=opp.team_name,
        my_ppw=my_ppw, opp_ppw=opp_ppw, margin=margin,
        win_prob_band=band, confidence_note=conf_note,
        grid=grid,
        my_needs=list(lev.my_needs), my_surplus_positions=list(lev.my_surplus_positions),
        opp_needs=list(lev.opp_needs), opp_surplus_positions=list(lev.opp_surplus_positions),
        their_surplus_my_needs=list(lev.their_surplus_my_needs),
        my_surplus_their_needs=list(lev.my_surplus_their_needs),
        is_reciprocal_fit=lev.is_reciprocal_fit,
        start_sit=start_sit_out,
    )


def _build_start_sit_out(acting, values, def_grades, nfl_opp, rules) -> StartSitOut:
    ss = build_start_sit(acting, values, def_grades, nfl_opp, rules)
    return StartSitOut(
        starters=[StarterMatchupOut(
            name=s.name, position=s.position, slot=s.slot, nfl_team=s.nfl_team,
            opponent=s.opponent, grade=s.grade, injury_flag=s.injury_flag,
            forward_ppg=s.forward_ppg, unfillable=s.unfillable, unfillable_reason=s.unfillable_reason,
        ) for s in ss.starters],
        swaps=[BenchSwapOut(
            position=w.position, starter_name=w.starter_name, starter_grade=w.starter_grade,
            bench_name=w.bench_name, bench_opponent=w.bench_opponent, bench_grade=w.bench_grade,
            reason=w.reason,
        ) for w in ss.swaps],
        replacements=[ReplacementOut(
            out_name=r.out_name, out_status=r.out_status, position=r.position, in_name=r.in_name,
        ) for r in ss.replacements],
        covered_positions=list(ss.covered_positions),
    )
