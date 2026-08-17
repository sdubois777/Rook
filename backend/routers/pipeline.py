"""
Pipeline router — manual triggers for each agent stage via HTTP POST.
Used for development, re-runs, and spot-checks. Not called by the UI.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.core.dependencies import require_admin
from backend.middleware.rate_limit import rate_limit_pipeline
from backend.utils.seasons import asof_now

logger = logging.getLogger(__name__)

# How old a market price refresh may be before the status endpoint calls it stale.
# Every value gap on the board is our bid ceiling minus these prices, and prices move
# daily through training camp, so a week is already a long time before draft day.
MARKET_VALUE_STALE_DAYS = 7

# Operator-only router: every route triggers agent runs / scrapes / syncs that cost
# real money and time. ADMIN auth gates the whole router (a regular logged-in user
# must not be able to start a run); the expensive POST triggers additionally carry a
# per-IP rate cap. Stephen's normal workflow is the CLI (scripts/run_predraft_pipeline.py),
# which bypasses HTTP entirely and is unaffected.
router = APIRouter(
    prefix="/pipeline",
    tags=["pipeline"],
    dependencies=[Depends(require_admin)],
)

# Applied per-trigger so the cheap GET status read isn't throttled with the runs.
_ratelimit = Depends(rate_limit_pipeline)


class PipelineResponse(BaseModel):
    status: str
    message: str
    details: dict | list = {}


@router.post("/run-team-systems", response_model=PipelineResponse, dependencies=[_ratelimit])
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


@router.post("/run-roster-changes", response_model=PipelineResponse, dependencies=[_ratelimit])
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


@router.post("/run-roster-changes/{team_abbr}", response_model=PipelineResponse, dependencies=[_ratelimit])
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


@router.post("/run-team-systems/{team_abbr}", response_model=PipelineResponse, dependencies=[_ratelimit])
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


@router.post("/sync-yahoo-players", response_model=PipelineResponse, dependencies=[_ratelimit])
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


@router.post("/refresh-market-values", response_model=PipelineResponse, dependencies=[_ratelimit])
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

    # RECOMPUTE what the new price invalidates — and ONLY that. The sync writes just the
    # price columns, but value_gap, value_gap_signal, value_assessment, pay_up_flag,
    # nomination_target_flag and signal_conviction are DERIVED from the price and stored,
    # not computed at read time (a PPR board passes the stored values straight through —
    # see resolve_market_and_gap in backend/services/format_display.py). Without this the
    # board printed a fresh price beside a gap computed from the price it replaced.
    #
    # run_valuation_pass is deliberately NOT called here — see the longer note in
    # scripts/refresh_market_values.py. It rewrites bid ceilings, which a market move did
    # not change, and re-running it outside the pipeline reprices the board on different
    # terms from the pipeline's own call.
    revalued = None
    if result.get("matched"):
        from backend.engines.valuation import reconcile_value_signals
        rec = await reconcile_value_signals()
        revalued = {"signals": rec.get("updated")}
        result["revalued"] = revalued

    if result.get("error"):
        return PipelineResponse(
            status="failed",
            message=f"Market value sync did not run: {result['error']}",
            details=result,
        )

    year = result.get("year")
    is_current = result.get("is_current_season")
    season_label = "current" if is_current else "previous"
    revalue_note = (
        f", reconciled {revalued['signals']} market-relative signal(s)"
        if revalued else ""
    )

    return PipelineResponse(
        status="complete",
        message=(
            f"Market values synced — "
            f"{result['matched']} matched, {result['unmatched']} unmatched, "
            f"{result.get('ambiguous', 0)} skipped as ambiguous "
            f"({year} {season_label} season){revalue_note}"
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

    # WARN ON AGE, not on the season flag. The old check was `if not is_current_season`,
    # but get_fantasypros_auction_year (backend/utils/seasons.py) now returns True
    # unconditionally, which is written straight into this column — so the branch could
    # never fire and this endpoint reported a clean status for a refresh of any age.
    # Measured on the development database before the price refresh was added to the
    # pipeline: the newest refresh was 91 days old and this endpoint said nothing.
    # asof_now(), not datetime.now(): CLAUDE.md requires every clock read to go through
    # the as-of helpers, so that a past-dated run reports the age the prices had on that
    # date rather than their age today.
    now = asof_now()
    age_days = None
    note = None
    if row.refreshed_at is not None:
        refreshed = row.refreshed_at
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=timezone.utc)
        age_days = (now - refreshed).days
        if age_days >= MARKET_VALUE_STALE_DAYS:
            note = (
                f"Market prices are {age_days} days old (last refreshed "
                f"{refreshed.date()}). Every value gap is computed against these "
                f"prices. Re-run the pre-draft pipeline, or the market value refresh "
                f"on its own, before trusting them."
            )
    elif not row.is_current_season:
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
        "age_days": age_days,
        "is_stale": bool(age_days is not None and age_days >= MARKET_VALUE_STALE_DAYS),
        "note": note,
    }





@router.post("/import-league-auction", response_model=PipelineResponse, dependencies=[_ratelimit])
async def import_league_auction(
    csv_path: str = Query(..., description="Path to CSV file from Yahoo Draft Recap"),
    year: int = Query(..., description="Season year the auction took place"),
):
    """
    Import league auction history from a CSV file (Yahoo Draft Recap copy-paste).
    After import, refreshes market_value_league on matched players.
    """
    from pathlib import Path

    from backend.database import AsyncSessionLocal
    from backend.engines.league_auction import import_league_auction_csv, refresh_market_value_league

    path = Path(csv_path)
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"CSV file not found: {csv_path}")

    async with AsyncSessionLocal() as session:
        result = await import_league_auction_csv(session, csv_path, year)
        refresh = await refresh_market_value_league(session, year)

    return PipelineResponse(
        status="complete",
        message=(
            f"League auction import: {result['matched']} matched, "
            f"{result['unmatched']} unmatched. "
            f"Refreshed market_value_league for {refresh['updated']} players."
        ),
        details={**result, "refresh": refresh},
    )


@router.post("/rematch-auction-history", response_model=PipelineResponse, dependencies=[_ratelimit])
async def rematch_auction_history():
    """
    Re-match league_auction_history rows with player_id=NULL to existing players.
    Useful after adding new players (e.g. rookies) to the database.
    Automatically refreshes market_value_league after re-matching.
    """
    from backend.database import AsyncSessionLocal
    from backend.engines.league_auction import (
        rematch_unmatched_auction_history,
        refresh_market_value_league,
    )

    async with AsyncSessionLocal() as session:
        result = await rematch_unmatched_auction_history(session)
        refresh = await refresh_market_value_league(session)

    return PipelineResponse(
        status="complete",
        message=(
            f"Re-matched {result['rematched']} auction history rows "
            f"({result['still_unmatched']} still unmatched). "
            f"Refreshed market_value_league for {refresh['updated']} players."
        ),
        details={**result, "refresh": refresh},
    )


@router.post("/sync-league-history", response_model=PipelineResponse, dependencies=[_ratelimit])
async def sync_league_history():
    """
    Auto-discover all historical auction leagues via Yahoo API and pull draft results.
    Syncs all past seasons that haven't been synced yet.
    Refreshes market_value_league from the latest year after sync.
    """
    from backend.database import AsyncSessionLocal
    from backend.engines.league_auction import sync_all_league_history

    async with AsyncSessionLocal() as session:
        result = await sync_all_league_history(session)

    synced = result.get("synced_seasons", [])
    skipped = result.get("skipped_seasons", [])
    errors = result.get("errors", [])

    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])

    return PipelineResponse(
        status="complete",
        message=(
            f"League history sync: {len(synced)} seasons synced, "
            f"{len(skipped)} skipped, {result.get('total_picks', 0)} total picks. "
            f"{len(errors)} errors."
        ),
        details=result,
    )
