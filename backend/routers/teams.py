"""
Teams router — NFL team intelligence and system grades.

Endpoints:
  GET /teams         — all 32 teams with system grades
  GET /teams/{abbr}  — team detail with skill position players
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from backend.core.dependencies import get_db
from backend.models.team_system import TeamSystem
from backend.repositories.player_repo import PlayerRepository
from backend.repositories.team_system_repo import TeamSystemRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/teams", tags=["teams"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class TeamSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    team_abbr: str
    system_grade: Optional[str] = None
    qb_name: Optional[str] = None
    qb_tier: Optional[str] = None
    pass_protection_grade: Optional[str] = None
    run_blocking_grade: Optional[str] = None
    oc_scheme: Optional[str] = None
    rookie_qb_flag: bool = False
    compound_risk_flag: bool = False
    player_count: int = 0


class TeamListResponse(BaseModel):
    teams: list[TeamSummary]


class TeamPlayerSummary(BaseModel):
    id: str
    name: str
    position: Optional[str] = None
    tier: Optional[int] = None
    recommended_bid_ceiling: Optional[float] = None
    market_value: Optional[float] = None
    value_gap: Optional[float] = None
    breakout_flag: bool = False
    top_flag: Optional[str] = None


class TeamDetail(BaseModel):
    team_abbr: str
    season_year: Optional[int] = None
    system_grade: Optional[str] = None
    system_ceiling: Optional[str] = None
    notes: Optional[str] = None

    # QB
    qb_name: Optional[str] = None
    qb_tier: Optional[str] = None
    qb_experience_years: Optional[int] = None
    qb_cpoe: Optional[float] = None
    qb_air_yards_per_attempt: Optional[float] = None
    qb_pressure_performance: Optional[str] = None
    qb_downfield_aggressiveness: Optional[str] = None
    rookie_qb_flag: bool = False
    compound_risk_flag: bool = False
    qb_wr_trust_score: Optional[int] = None

    # O-line
    pass_protection_grade: Optional[str] = None
    run_blocking_grade: Optional[str] = None

    # OC
    oc_name: Optional[str] = None
    oc_scheme: Optional[str] = None
    oc_run_pass_split_tendency: Optional[float] = None
    personnel_tendency: Optional[str] = None
    red_zone_philosophy: Optional[str] = None

    # Players
    players: list[TeamPlayerSummary] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calculate_qb_wr_trust_score(ts: TeamSystem) -> int:
    """
    Simple 0-100 trust score for QB→WR target volume.
    Based on: QB tier, pass rate tendency, O-line grade.
    """
    base = 50
    tier_map = {"elite": 25, "solid": 15, "average": 5, "below_average": -10, "poor": -20}
    base += tier_map.get(ts.qb_tier or "", 0)

    tendency = (ts.oc_run_pass_split_tendency or 0.5)
    if tendency >= 0.55:
        base += 15
    elif tendency <= 0.45:
        base -= 15

    a_grades = {"A+", "A", "A-"}
    d_grades = {"D+", "D", "D-", "F"}
    if ts.pass_protection_grade in a_grades:
        base += 10
    elif ts.pass_protection_grade in d_grades:
        base -= 10

    return max(0, min(100, base))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=TeamListResponse)
async def list_teams(
    sort: str = "system_grade",
    order: str = "desc",
    db=Depends(get_db),
) -> TeamListResponse:
    """All 32 teams with latest system grades and player counts."""
    team_systems = await TeamSystemRepository(db).list_latest()
    counts = await PlayerRepository(db).count_by_team()

    teams = []
    for ts in team_systems:
        teams.append(TeamSummary(
            team_abbr=ts.team_abbr,
            system_grade=ts.system_grade,
            qb_name=ts.qb_name,
            qb_tier=ts.qb_tier,
            pass_protection_grade=ts.pass_protection_grade,
            run_blocking_grade=ts.run_blocking_grade,
            oc_scheme=ts.oc_scheme,
            rookie_qb_flag=ts.rookie_qb_flag or False,
            compound_risk_flag=ts.compound_risk_flag or False,
            player_count=counts.get(ts.team_abbr, 0),
        ))

    # Sort
    grade_order = {"A+": 0, "A": 1, "A-": 2, "B+": 3, "B": 4, "B-": 5,
                   "C+": 6, "C": 7, "C-": 8, "D+": 9, "D": 10, "D-": 11, "F": 12}
    if sort == "system_grade":
        teams.sort(
            key=lambda t: grade_order.get(t.system_grade or "F", 12),
            reverse=(order == "desc"),
        )
    elif sort == "team":
        teams.sort(key=lambda t: t.team_abbr, reverse=(order == "desc"))

    return TeamListResponse(teams=teams)


@router.get("/{abbr}", response_model=TeamDetail)
async def get_team(abbr: str, db=Depends(get_db)) -> TeamDetail:
    """Team detail with full system context and skill position players."""
    team_abbr = abbr.upper()

    ts = await TeamSystemRepository(db).get_latest_for_team(team_abbr)
    if not ts:
        raise HTTPException(status_code=404, detail=f"Team not found: {team_abbr}")

    players = await PlayerRepository(db).list_skill_players_for_team(team_abbr)

    team_players = []
    for p in players:
        top_flag = None
        if p.dependencies:
            # Pick the highest-impact flag
            sorted_deps = sorted(
                p.dependencies,
                key=lambda d: abs(float(d.value_impact_pct or 0)),
                reverse=True,
            )
            if sorted_deps:
                top_flag = sorted_deps[0].flag_type

        team_players.append(TeamPlayerSummary(
            id=str(p.id),
            name=p.name,
            position=p.position,
            tier=p.tier,
            recommended_bid_ceiling=float(p.recommended_bid_ceiling) if p.recommended_bid_ceiling else None,
            market_value=float(p.market_value) if p.market_value else None,
            value_gap=float(p.value_gap) if p.value_gap else None,
            breakout_flag=p.breakout_flag or False,
            top_flag=top_flag,
        ))

    return TeamDetail(
        team_abbr=ts.team_abbr,
        season_year=ts.season_year,
        system_grade=ts.system_grade,
        system_ceiling=ts.system_ceiling,
        notes=ts.notes,
        qb_name=ts.qb_name,
        qb_tier=ts.qb_tier,
        qb_experience_years=ts.qb_experience_years,
        qb_cpoe=float(ts.qb_cpoe) if ts.qb_cpoe else None,
        qb_air_yards_per_attempt=float(ts.qb_air_yards_per_attempt) if ts.qb_air_yards_per_attempt else None,
        qb_pressure_performance=ts.qb_pressure_performance,
        qb_downfield_aggressiveness=ts.qb_downfield_aggressiveness,
        rookie_qb_flag=ts.rookie_qb_flag or False,
        compound_risk_flag=ts.compound_risk_flag or False,
        qb_wr_trust_score=_calculate_qb_wr_trust_score(ts),
        pass_protection_grade=ts.pass_protection_grade,
        run_blocking_grade=ts.run_blocking_grade,
        oc_name=ts.oc_name,
        oc_scheme=ts.oc_scheme,
        oc_run_pass_split_tendency=float(ts.oc_run_pass_split_tendency) if ts.oc_run_pass_split_tendency else None,
        personnel_tendency=ts.personnel_tendency,
        red_zone_philosophy=ts.red_zone_philosophy,
        players=team_players,
    )
