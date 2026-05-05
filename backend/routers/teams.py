"""
Teams router — NFL team intelligence and system grades.

Endpoints:
  GET /teams         — all 32 teams with system grades
  GET /teams/{abbr}  — team detail with skill position players
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from backend.database import AsyncSessionLocal
from backend.models.player import Player
from backend.models.team_system import TeamSystem

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
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=TeamListResponse)
async def list_teams(sort: str = "system_grade", order: str = "desc"):
    """All 32 teams with latest system grades and player counts."""
    async with AsyncSessionLocal() as session:
        # Get latest team system per team
        subq = (
            select(
                TeamSystem.team_abbr,
                func.max(TeamSystem.season_year).label("max_year"),
            )
            .group_by(TeamSystem.team_abbr)
            .subquery()
        )

        result = await session.execute(
            select(TeamSystem)
            .join(
                subq,
                (TeamSystem.team_abbr == subq.c.team_abbr)
                & (TeamSystem.season_year == subq.c.max_year),
            )
        )
        team_systems = result.scalars().all()

        # Get player counts per team
        count_result = await session.execute(
            select(Player.team_abbr, func.count(Player.id))
            .where(Player.team_abbr.isnot(None))
            .group_by(Player.team_abbr)
        )
        counts = dict(count_result.all())

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
async def get_team(abbr: str):
    """Team detail with full system context and skill position players."""
    team_abbr = abbr.upper()

    async with AsyncSessionLocal() as session:
        # Get latest team system
        result = await session.execute(
            select(TeamSystem)
            .where(TeamSystem.team_abbr == team_abbr)
            .order_by(TeamSystem.season_year.desc())
            .limit(1)
        )
        ts = result.scalar_one_or_none()

        if not ts:
            raise HTTPException(status_code=404, detail=f"Team not found: {team_abbr}")

        # Get skill position players
        player_result = await session.execute(
            select(Player)
            .where(Player.team_abbr == team_abbr)
            .where(Player.position.in_(["QB", "RB", "WR", "TE"]))
            .options(selectinload(Player.dependencies))
            .order_by(Player.recommended_bid_ceiling.desc().nulls_last())
        )
        players = player_result.scalars().all()

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
        pass_protection_grade=ts.pass_protection_grade,
        run_blocking_grade=ts.run_blocking_grade,
        oc_name=ts.oc_name,
        oc_scheme=ts.oc_scheme,
        oc_run_pass_split_tendency=float(ts.oc_run_pass_split_tendency) if ts.oc_run_pass_split_tendency else None,
        personnel_tendency=ts.personnel_tendency,
        red_zone_philosophy=ts.red_zone_philosophy,
        players=team_players,
    )
