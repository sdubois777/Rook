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


class DryRunAgentEstimate(BaseModel):
    agent_name: str
    estimated_entities: int = 0
    estimated_haiku_calls: int = 0
    estimated_sonnet_calls: int = 0
    estimated_cost_usd: float = 0.0


class DryRunResponse(BaseModel):
    estimates: list[DryRunAgentEstimate]
    total_estimated_cost_usd: float = 0.0
    disclaimer: str = "Estimate only. Actual cost depends on cache hits."


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


@router.post("/pipeline/dry-run", response_model=DryRunResponse)
async def pipeline_dry_run(body: PipelineRunRequest):
    """Estimate cost of running specified agent(s) without actually running them."""
    from backend.models.player import Player, PlayerProfile
    from backend.models.team_system import TeamSystem

    agent = body.agent_name
    if agent not in KNOWN_AGENTS:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {agent}")

    async with AsyncSessionLocal() as session:
        estimates = []

        if agent == "player_profiles":
            # Player profiles batches by team (32 calls).
            # Each team call uses Haiku, but top-tier players get a
            # second Sonnet call (~15% of players).
            total_result = await session.execute(
                select(func.count(Player.id))
                .where(Player.position.in_(["QB", "RB", "WR", "TE"]))
            )
            total_players = total_result.scalar() or 0
            team_count = 32 if not body.team_abbr else 1
            sonnet_calls = max(1, int(total_players * 0.15))  # ~15% top-tier get Sonnet
            haiku_calls = team_count  # one Haiku call per team
            cost = sonnet_calls * 0.003 + haiku_calls * 0.0003

            estimates.append(DryRunAgentEstimate(
                agent_name="player_profiles",
                estimated_entities=total_players,
                estimated_sonnet_calls=sonnet_calls,
                estimated_haiku_calls=haiku_calls,
                estimated_cost_usd=round(cost, 4),
            ))
        elif agent in ("team_systems", "roster_changes", "injury_risk", "schedule"):
            team_count = 32
            if body.team_abbr:
                team_count = 1
            is_sonnet = agent == "roster_changes"
            calls = team_count
            cost_per = 0.003 if is_sonnet else 0.0003
            estimates.append(DryRunAgentEstimate(
                agent_name=agent,
                estimated_entities=team_count,
                estimated_sonnet_calls=calls if is_sonnet else 0,
                estimated_haiku_calls=0 if is_sonnet else calls,
                estimated_cost_usd=round(calls * cost_per, 4),
            ))
        elif agent == "beat_reporter":
            estimates.append(DryRunAgentEstimate(
                agent_name="beat_reporter",
                estimated_entities=1,
                estimated_haiku_calls=1,
                estimated_cost_usd=0.0003,
            ))

    total_cost = sum(e.estimated_cost_usd for e in estimates)
    return DryRunResponse(
        estimates=estimates,
        total_estimated_cost_usd=round(total_cost, 4),
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
