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
from backend.services.trade.lineup import DEFAULT_LINEUP_RULES, lineup_strength_ppg, roster_strength
from backend.services.trade.trade_demo_source import trade_demo_enabled, trade_demo_enforce_gates
from backend.services.trade.trade_proposals import _lineup_roster, analyze_roster
from backend.services.trade.value_engine import replacement_ppg_by_position

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
    demo = trade_demo_enabled()
    if not demo:
        raise HTTPException(
            status_code=404,
            detail="matchup demo league is only available under TRADE_DEMO_MODE",
        )

    # SAME seam + SAME evaluate_league (incl. the slice-4 DST tilt) as /trade/league,
    # so every number is consistent across pages. This path touches ONLY pure
    # primitives — no agent, no credit deduction.
    from backend.routers.trade import load_league_for_analysis
    state, values, _ = await load_league_for_analysis(db, user, demo)

    rules = DEFAULT_LINEUP_RULES
    replacement = replacement_ppg_by_position(values)

    # Resolve the acting team.
    acting = None
    if my_team_id:
        acting = next((t for t in state.teams if t.team_id == my_team_id), None)
        if acting is None:
            raise HTTPException(status_code=400, detail=f"team {my_team_id!r} not in league")
    acting = acting or state.my_team or (state.teams[0] if state.teams else None)
    if acting is None:
        raise HTTPException(status_code=400, detail="no team to scout for")

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

    # --- scout the acting team's opponent ---
    scout = None
    opp_id = opponent_of(matchups, acting.team_id)
    if opp_id is not None:
        opp = next((t for t in state.teams if t.team_id == opp_id), None)
        if opp is not None:
            scout = _scout(acting, opp, values, rules, replacement)

    return MatchupLeagueResponse(
        season=state.season, week=state.week,
        my_team_id=acting.team_id, my_team_name=acting.team_name,
        teams=ladder, matchups=pairs, scout=scout,
        demo_mode=demo, enforced=trade_demo_enforce_gates(),
    )


def _scout(acting, opp, values, rules, replacement) -> ScoutOut:
    my_roster = _lineup_roster(acting, values)
    opp_roster = _lineup_roster(opp, values)

    my_ppw = lineup_strength_ppg(my_roster, rules, replacement)
    opp_ppw = lineup_strength_ppg(opp_roster, rules, replacement)
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

    my_grid = positional_slot_ppg(my_roster, rules, replacement)
    opp_grid = positional_slot_ppg(opp_roster, rules, replacement)
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
    )
