"""
Draft board router — tiered player rankings with strategy highlighting.

Endpoints:
  GET /draftboard  — all ranked players grouped by tier with strategy mode
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.core.dependencies import get_current_user
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile
from backend.models.dependency import PlayerDependency

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/draftboard", tags=["draftboard"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class DraftBoardFlag(BaseModel):
    flag_type: str
    trigger_player_name: Optional[str] = None
    confidence: Optional[str] = None


class DraftBoardPlayer(BaseModel):
    id: str
    name: str
    team_abbr: Optional[str] = None
    position: Optional[str] = None
    tier: Optional[int] = None
    recommended_bid_ceiling: Optional[float] = None
    baseline_value: Optional[float] = None
    market_value: Optional[float] = None
    market_value_league: Optional[float] = None
    value_gap: Optional[float] = None
    value_gap_signal: Optional[str] = None
    breakout_flag: bool = False
    is_rookie: bool = False
    ppr_points: Optional[float] = None
    injury_risk_level: Optional[str] = None
    ai_bid_ceiling: Optional[int] = None
    pay_up_flag: bool = False
    nomination_target_flag: bool = False
    flags: list[DraftBoardFlag] = []
    strategy_highlight: Optional[str] = None  # "primary" / "secondary" / "dimmed" / None


class DraftBoardResponse(BaseModel):
    tiers: dict[str, list[DraftBoardPlayer]]
    strategy: Optional[str] = None
    total_players: int = 0


# ---------------------------------------------------------------------------
# Strategy highlighting logic
# ---------------------------------------------------------------------------

def _apply_strategy(player: DraftBoardPlayer, strategy: str) -> str | None:
    """Determine highlight for a player given strategy mode."""
    pos = player.position
    tier = player.tier

    if strategy == "hero_rb":
        if pos == "RB" and tier == 1:
            return "primary"
        if pos == "WR" and tier in (1, 2):
            return "secondary"
        return None

    if strategy == "zero_rb":
        if pos == "WR" and tier in (1, 2):
            return "primary"
        if pos == "TE" and tier == 1:
            return "secondary"
        if pos == "RB":
            return "dimmed"
        return None

    if strategy == "stars_and_scrubs":
        if tier == 1:
            return "primary"
        if tier >= 4:
            return "secondary"
        return None

    # "balanced" — no highlighting
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=DraftBoardResponse)
async def get_draftboard(
    position: Optional[str] = None,
    tier: Optional[int] = None,
    strategy: Optional[str] = None,
    _user=Depends(get_current_user),
):
    """Ranked players grouped by tier with optional strategy highlighting."""
    async with AsyncSessionLocal() as session:
        query = (
            select(Player)
            .where(Player.recommended_bid_ceiling.isnot(None))
            .options(
                selectinload(Player.dependencies),
                selectinload(Player.injury_profile),
                selectinload(Player.profile),
            )
        )

        if position:
            query = query.where(Player.position == position.upper())
        if tier is not None:
            query = query.where(Player.tier == tier)

        query = query.order_by(
            Player.tier.asc().nulls_last(),
            Player.recommended_bid_ceiling.desc().nulls_last(),
        )

        result = await session.execute(query)
        players = result.scalars().all()

    # Build response grouped by tier
    tiers: dict[str, list[DraftBoardPlayer]] = {}
    total = 0

    for p in players:
        flags = []
        for dep in (p.dependencies or []):
            flags.append(DraftBoardFlag(
                flag_type=dep.flag_type,
                trigger_player_name=dep.trigger_player_name,
                confidence=dep.confidence,
            ))

        dbp = DraftBoardPlayer(
            id=str(p.id),
            name=p.name,
            team_abbr=p.team_abbr,
            position=p.position,
            tier=p.tier,
            recommended_bid_ceiling=float(p.recommended_bid_ceiling) if p.recommended_bid_ceiling else None,
            baseline_value=float(p.baseline_value) if p.baseline_value else None,
            market_value=float(p.market_value) if p.market_value else None,
            market_value_league=float(p.market_value_league) if p.market_value_league else None,
            value_gap=float(p.value_gap) if p.value_gap else None,
            value_gap_signal=p.value_gap_signal,
            ppr_points=(
                float(p.profile.clean_season_baseline.get("ppr_points"))
                if p.profile and p.profile.clean_season_baseline
                and p.profile.clean_season_baseline.get("ppr_points") is not None
                else None
            ),
            breakout_flag=p.breakout_flag or False,
            is_rookie=p.is_rookie or False,
            injury_risk_level=p.injury_profile.overall_risk_level if p.injury_profile else None,
            ai_bid_ceiling=p.ai_bid_ceiling,
            pay_up_flag=p.pay_up_flag or False,
            nomination_target_flag=p.nomination_target_flag or False,
            flags=flags,
            strategy_highlight=None,
        )

        # Apply strategy
        if strategy and strategy in ("hero_rb", "zero_rb", "stars_and_scrubs", "balanced"):
            dbp.strategy_highlight = _apply_strategy(dbp, strategy)

        tier_key = str(p.tier or 0)
        if tier_key not in tiers:
            tiers[tier_key] = []
        tiers[tier_key].append(dbp)
        total += 1

    return DraftBoardResponse(
        tiers=tiers,
        strategy=strategy,
        total_players=total,
    )
