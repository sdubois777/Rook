"""
Pipeline router — manual triggers for each agent stage via HTTP POST.
Used for development, re-runs, and spot-checks. Not called by the UI.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pipeline", tags=["pipeline"])


class PipelineResponse(BaseModel):
    status: str
    message: str
    details: dict | list = {}


@router.post("/run-team-systems", response_model=PipelineResponse)
async def run_team_systems(background_tasks: BackgroundTasks):
    """
    Trigger Team Systems Agent for all 32 NFL teams.
    Runs in the background — returns immediately.
    Check logs for progress.
    """
    from backend.agents.team_systems import run_all_teams

    async def _run():
        results = await run_all_teams(concurrency=4)
        success = sum(1 for v in results.values() if v)
        logger.info("Team Systems pipeline finished: %d/32 succeeded", success)

    background_tasks.add_task(_run)
    return PipelineResponse(
        status="started",
        message="Team Systems Agent running for all 32 teams. Check logs for progress.",
    )


@router.post("/run-roster-changes", response_model=PipelineResponse)
async def run_roster_changes(background_tasks: BackgroundTasks):
    """Trigger Roster Changes Agent for all 32 teams. Runs in background."""
    from backend.agents.roster_changes import run_all_teams

    async def _run():
        results = await run_all_teams(concurrency=3)
        total = sum(results.values())
        logger.info("Roster Changes pipeline finished: %d total flags", total)

    background_tasks.add_task(_run)
    return PipelineResponse(
        status="started",
        message="Roster Changes Agent running for all 32 teams.",
    )


@router.post("/run-roster-changes/{team_abbr}", response_model=PipelineResponse)
async def run_roster_changes_single(team_abbr: str):
    """Run Roster Changes Agent for a single team synchronously."""
    from backend.agents.roster_changes import run_for_team
    from backend.agents.team_systems import NFL_TEAMS

    team = team_abbr.upper()
    if team not in NFL_TEAMS:
        raise HTTPException(status_code=400, detail=f"Unknown team: {team_abbr}")

    flags = await run_for_team(team)
    return PipelineResponse(
        status="complete",
        message=f"Roster Changes Agent completed for {team}: {len(flags)} flags",
        details=flags,
    )


@router.post("/run-team-systems/{team_abbr}", response_model=PipelineResponse)
async def run_team_systems_single(team_abbr: str):
    """
    Run Team Systems Agent for a single team. Runs synchronously — blocks until done.
    Good for spot-checking individual teams.
    """
    from backend.agents.team_systems import run_for_team, NFL_TEAMS

    team = team_abbr.upper()
    if team not in NFL_TEAMS:
        raise HTTPException(status_code=400, detail=f"Unknown team: {team_abbr}")

    data = await run_for_team(team)
    if data is None:
        raise HTTPException(status_code=500, detail=f"Agent failed for {team} — check logs")

    return PipelineResponse(
        status="complete",
        message=f"Team Systems Agent completed for {team}",
        details=data,
    )


@router.post("/sync-yahoo-players", response_model=PipelineResponse)
async def sync_yahoo_players():
    """
    Pull Yahoo player universe and match IDs to draft bible records.
    Updates yahoo_player_id on matched player rows.
    Requires YAHOO_REFRESH_TOKEN to be set in .env.
    """
    from backend.database import AsyncSessionLocal
    from backend.integrations.yahoo_api import sync_yahoo_player_ids

    async with AsyncSessionLocal() as session:
        result = await sync_yahoo_player_ids(session)

    return PipelineResponse(
        status="complete",
        message=(
            f"Yahoo player sync done — "
            f"{result['matched']} matched, {result['unmatched']} unmatched"
        ),
        details=result,
    )


@router.post("/refresh-market-values", response_model=PipelineResponse)
async def refresh_market_values():
    """
    Scrape FantasyPros auction values and update market_value fields on players.
    Uses Playwright (slow) — runs synchronously so caller sees results.
    Automatically determines best year (current if July+, previous otherwise).
    """
    from backend.database import AsyncSessionLocal
    from backend.engines.market_values import sync_market_values

    async with AsyncSessionLocal() as session:
        result = await sync_market_values(session, scoring_format="ppr")

    year = result.get("year")
    is_current = result.get("is_current_season")
    season_label = "current" if is_current else "previous"

    return PipelineResponse(
        status="complete",
        message=(
            f"Market values synced — "
            f"{result['matched']} matched, {result['unmatched']} unmatched "
            f"({year} {season_label} season)"
        ),
        details=result,
    )


@router.get("/market-values/status")
async def market_values_status():
    """
    Return the most recent market value refresh metadata.
    Used by the frontend to show source year and staleness warnings.
    """
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.models.market_value_metadata import MarketValueMetadata

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(MarketValueMetadata)
                .order_by(MarketValueMetadata.refreshed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if row is None:
        return {
            "source": None,
            "year": None,
            "is_current_season": None,
            "player_count": 0,
            "refreshed_at": None,
            "note": "No market values loaded yet — run the refresh pipeline",
        }

    note = None
    if not row.is_current_season:
        note = (
            f"Using {row.year} data — "
            f"refresh in July when {row.year + 1} data is available"
        )

    return {
        "source": row.source,
        "year": row.year,
        "is_current_season": row.is_current_season,
        "player_count": row.player_count,
        "refreshed_at": row.refreshed_at.isoformat() if row.refreshed_at else None,
        "note": note,
    }


@router.post("/sync-league-settings", response_model=PipelineResponse)
async def sync_league_settings():
    """
    Pull league settings from Yahoo and upsert into league_settings table.
    Requires YAHOO_REFRESH_TOKEN to be set in .env.
    """
    from backend.database import AsyncSessionLocal
    from backend.integrations.yahoo_api import sync_league_settings as _sync

    async with AsyncSessionLocal() as session:
        result = await _sync(session)

    return PipelineResponse(
        status="complete",
        message=f"League settings synced — {result['league_name']} ({result['scoring_format']}, {result['team_count']} teams)",
        details=result,
    )
