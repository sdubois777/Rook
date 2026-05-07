"""
Admin router — pipeline status, trigger runs, and cost reporting.

Endpoints:
  GET  /admin/pipeline-status  — freshness of each agent's last run
  POST /admin/pipeline/run     — trigger a pipeline agent run
  GET  /admin/cost-report      — API usage costs grouped by agent
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.database import AsyncSessionLocal
from backend.models.agent_cache import AgentCache
from backend.models.api_usage_log import ApiUsageLog

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class AgentStatus(BaseModel):
    agent_name: str
    last_run: Optional[str] = None
    entity_count: int = 0
    stale: bool = False  # True if last run > 7 days ago


class PipelineStatusResponse(BaseModel):
    agents: list[AgentStatus]


class PipelineRunRequest(BaseModel):
    agent_name: str
    team_abbr: Optional[str] = None  # Run for specific team only


class PipelineRunResponse(BaseModel):
    status: str
    message: str


class AgentCostSummary(BaseModel):
    agent_name: str
    total_calls: int = 0
    cache_hits: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0


class CostReportResponse(BaseModel):
    agents: list[AgentCostSummary]
    grand_total_usd: float = 0.0
    period_days: int = 30


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

KNOWN_AGENTS = [
    "team_systems",
    "roster_changes",
    "player_profiles",
    "injury_risk",
    "schedule",
    "beat_reporter",
]


@router.get("/pipeline-status", response_model=PipelineStatusResponse)
async def get_pipeline_status():
    """Freshness of each agent's last run."""
    now = datetime.now(timezone.utc)
    stale_threshold = now - timedelta(days=7)

    async with AsyncSessionLocal() as session:
        agents = []
        for agent_name in KNOWN_AGENTS:
            # Get most recent cache entry
            result = await session.execute(
                select(
                    func.max(AgentCache.created_at),
                    func.count(AgentCache.id),
                )
                .where(AgentCache.agent_name == agent_name)
            )
            row = result.one()
            last_run = row[0]
            entity_count = row[1]

            agents.append(AgentStatus(
                agent_name=agent_name,
                last_run=last_run.isoformat() if last_run else None,
                entity_count=entity_count,
                stale=last_run < stale_threshold if last_run else True,
            ))

    return PipelineStatusResponse(agents=agents)


@router.post("/pipeline/run", response_model=PipelineRunResponse)
async def trigger_pipeline_run(body: PipelineRunRequest):
    """Trigger a pipeline agent run in the background."""
    if body.agent_name not in KNOWN_AGENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown agent. Must be one of: {KNOWN_AGENTS}",
        )

    import asyncio

    async def _run_agent():
        try:
            if body.agent_name == "team_systems":
                from backend.agents.team_systems import run_all_teams
                await run_all_teams()
            elif body.agent_name == "roster_changes":
                from backend.agents.roster_changes import run_all_teams
                await run_all_teams(concurrency=2)
            elif body.agent_name == "player_profiles":
                from backend.agents.player_profiles import run_all_teams
                await run_all_teams()
            elif body.agent_name == "injury_risk":
                from backend.agents.injury_risk import run_all_teams
                await run_all_teams()
            elif body.agent_name == "schedule":
                from backend.agents.schedule import run_all_teams
                await run_all_teams()
            elif body.agent_name == "beat_reporter":
                from backend.agents.beat_reporter import run
                await run()
            logger.info("Pipeline run completed: agent=%s", body.agent_name)
        except Exception as exc:
            logger.error("Pipeline run failed: agent=%s error=%s", body.agent_name, exc)

    # Fire and forget — run in background
    asyncio.create_task(_run_agent())

    logger.info(
        "Pipeline run started: agent=%s team=%s",
        body.agent_name,
        body.team_abbr or "all",
    )

    return PipelineRunResponse(
        status="started",
        message=f"Pipeline run started for {body.agent_name}"
        + (f" (team: {body.team_abbr})" if body.team_abbr else " (all teams)"),
    )


@router.get("/cost-report", response_model=CostReportResponse)
async def get_cost_report(days: int = 30):
    """API usage costs grouped by agent for the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                ApiUsageLog.agent_name,
                func.count(ApiUsageLog.id).label("total_calls"),
                func.count(ApiUsageLog.id).filter(
                    ApiUsageLog.cache_hit.is_(True)
                ).label("cache_hits"),
                func.coalesce(func.sum(ApiUsageLog.input_tokens), 0).label("total_input"),
                func.coalesce(func.sum(ApiUsageLog.output_tokens), 0).label("total_output"),
                func.coalesce(func.sum(ApiUsageLog.estimated_cost_usd), 0).label("total_cost"),
            )
            .where(ApiUsageLog.called_at >= cutoff)
            .group_by(ApiUsageLog.agent_name)
            .order_by(func.sum(ApiUsageLog.estimated_cost_usd).desc())
        )
        rows = result.all()

    agents = []
    grand_total = Decimal("0")

    for row in rows:
        cost = float(row.total_cost or 0)
        agents.append(AgentCostSummary(
            agent_name=row.agent_name,
            total_calls=row.total_calls or 0,
            cache_hits=row.cache_hits or 0,
            total_input_tokens=row.total_input or 0,
            total_output_tokens=row.total_output or 0,
            total_cost_usd=cost,
        ))
        grand_total += Decimal(str(cost))

    return CostReportResponse(
        agents=agents,
        grand_total_usd=float(grand_total),
        period_days=days,
    )
