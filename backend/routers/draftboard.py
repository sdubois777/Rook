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
from backend.schemas.player_badges import PlayerBadgeFields
from backend.repositories.player_repo import draftable_filter
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/draftboard", tags=["draftboard"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class DraftBoardFlag(BaseModel):
    flag_type: str
    trigger_player_name: Optional[str] = None
    confidence: Optional[str] = None


class DraftBoardPlayer(PlayerBadgeFields):
    id: str
    name: str
    team_abbr: Optional[str] = None
    position: Optional[str] = None
    tier: Optional[int] = None
    recommended_bid_ceiling: Optional[float] = None
    baseline_value: Optional[float] = None
    market_value: Optional[float] = None
    market_value_season: Optional[int] = None
    prior_season_price: Optional[float] = None
    prior_season_year: Optional[int] = None
    value_gap: Optional[float] = None
    value_gap_signal: Optional[str] = None
    breakout_flag: bool = False
    is_rookie: bool = False
    ppr_points: Optional[float] = None
    injury_risk_level: Optional[str] = None
    ai_bid_ceiling: Optional[int] = None
    pay_up_flag: bool = False
    nomination_target_flag: bool = False
    value_assessment: Optional[str] = None
    # Pre-draft availability discount (engines/availability.py): the value fields above
    # (recommended/ai_bid_ceiling/ppr_points) are ALREADY discounted by this factor for
    # a known multi-week absence; these expose it for the UI (badge/warning).
    availability_factor: float = 1.0
    availability_games_missed: int = 0
    # Snake-draft ADP (null until a pipeline run populates them — UI shows "--")
    adp_ai: Optional[float] = None
    adp_fantasypros: Optional[float] = None
    adp_scoring: Optional[str] = None
    adp_rank: Optional[int] = None
    adp_diff: Optional[float] = None
    snake_flag: Optional[str] = None
    round_num: Optional[int] = None  # (adp_rank-1)//team_count + 1
    flags: list[DraftBoardFlag] = []
    strategy_highlight: Optional[str] = None  # "primary" / "secondary" / "dimmed" / None


class DraftBoardResponse(BaseModel):
    tiers: dict[str, list[DraftBoardPlayer]]
    strategy: Optional[str] = None
    total_players: int = 0
    # Phase 2: the format the tier/points were read in + disclosure when a non-PPR league
    # is defaulted to PPR (unsupported/custom) or is seeing PPR ADP (per-format ADP not
    # yet populated). Auction $ figures stay on the PPR path regardless (dark).
    scoring_format: str = "ppr"
    scoring_format_defaulted: bool = False
    adp_format_defaulted: bool = False


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

_SNAKE_TEAM_COUNT = 12  # rounds = (adp_rank - 1) // team_count + 1


@router.get("", response_model=DraftBoardResponse)
async def get_draftboard(
    position: Optional[str] = None,
    tier: Optional[int] = None,
    strategy: Optional[str] = None,
    scoring_format: str = "ppr",
    draft_type: str = "auction",
    _user=Depends(get_current_user),
):
    """Ranked players. Auction: grouped by tier, sorted by bid ceiling. Snake:
    grouped by round, sorted by adp_rank (only players with an ADP rank).

    PRE-DRAFT surface → per-format TIER + projected POINTS read from player_format_values
    (PPR byte-identical). Auction $ stays dark; per-format ADP is read where a pipeline run
    has populated it, else PPR + disclosure."""
    from backend.services.format_display import (
        load_format_rows, overlay_for, resolve_scoring_format,
    )
    is_snake = draft_type == "snake"
    scoring_format, fmt_defaulted = resolve_scoring_format(scoring_format)

    async with AsyncSessionLocal() as session:
        query = (
            select(Player)
            # Snake needs a computed ADP rank; auction needs a bid ceiling.
            .where(
                Player.adp_rank.isnot(None)
                if is_snake
                else Player.recommended_bid_ceiling.isnot(None)
            )
            .where(draftable_filter())
            .options(
                selectinload(Player.dependencies),
                selectinload(Player.injury_profile),
                selectinload(Player.profile),
                selectinload(Player.historic_prices),
            )
        )

        if position:
            query = query.where(Player.position == position.upper())
        if tier is not None:
            query = query.where(Player.tier == tier)

        if is_snake:
            query = query.order_by(Player.adp_rank.asc().nulls_last())
        else:
            query = query.order_by(
                Player.tier.asc().nulls_last(),
                Player.recommended_bid_ceiling.desc().nulls_last(),
            )

        result = await session.execute(query)
        players = result.scalars().all()

        # Per-format overlay rows (empty for PPR → byte-identical).
        fmt_rows = await load_format_rows(session, [p.id for p in players], scoring_format)

    # Build response grouped by tier
    tiers: dict[str, list[DraftBoardPlayer]] = {}
    total = 0
    adp_format_defaulted = False

    prior_year = get_current_season() - 1

    for p in players:
        flags = []
        for dep in (p.dependencies or []):
            flags.append(DraftBoardFlag(
                flag_type=dep.flag_type,
                trigger_player_name=dep.trigger_player_name,
                confidence=dep.confidence,
            ))

        # Look up prior season price from historic table
        hist_price = None
        for hp in (p.historic_prices or []):
            if hp.season_year == prior_year:
                hist_price = float(hp.price)
                break

        # Pre-draft availability discount — the DRAFT-RANKED value is base × factor
        # (deterministic games-missed proration for a known multi-week absence). Base
        # columns are untouched in the DB; discounted here at read time (idempotent).
        avf = float(p.availability_factor) if p.availability_factor is not None else 1.0
        _raw_proj = (
            p.profile.clean_season_baseline.get("projected_ppr_season")
            or p.profile.clean_season_baseline.get("ppr_points")
        ) if (p.profile and p.profile.clean_season_baseline
              and (p.profile.clean_season_baseline.get("projected_ppr_season") is not None
                   or p.profile.clean_season_baseline.get("ppr_points") is not None)) else None

        # Per-format overlay (non-$ only): tier + projected points reprice by format; a
        # reception-dependent player tier-falls in Standard. $ fields below stay PPR (dark).
        ov = overlay_for(str(p.id), fmt_rows, scoring_format)
        eff_tier = ov.tier if ov.tier is not None else p.tier
        if ov.projected_points is not None:
            _raw_proj = ov.projected_points   # already the format's SEASON total
        if scoring_format != "ppr" and ov.adp_defaulted:
            adp_format_defaulted = True

        dbp = DraftBoardPlayer(
            id=str(p.id),
            name=p.name,
            team_abbr=p.team_abbr,
            position=p.position,
            tier=eff_tier,
            recommended_bid_ceiling=round(float(p.recommended_bid_ceiling) * avf, 1) if p.recommended_bid_ceiling else None,
            baseline_value=float(p.baseline_value) if p.baseline_value else None,
            market_value=float(p.market_value_fantasypros) if p.market_value_fantasypros else None,
            market_value_season=get_current_season() if p.market_value_fantasypros else None,
            prior_season_price=hist_price,
            prior_season_year=prior_year if hist_price else None,
            value_gap=float(p.value_gap) if p.value_gap else None,
            value_gap_signal=p.value_gap_signal,
            ppr_points=round(float(_raw_proj) * avf, 1) if _raw_proj is not None else None,
            breakout_flag=p.breakout_flag or False,
            is_rookie=p.is_rookie or False,
            injury_status=p.injury_status,
            injury_risk_level=p.injury_profile.overall_risk_level if p.injury_profile else None,
            availability_factor=avf,
            availability_games_missed=p.availability_games_missed or 0,
            ai_bid_ceiling=round(p.ai_bid_ceiling * avf) if p.ai_bid_ceiling else p.ai_bid_ceiling,
            pay_up_flag=p.pay_up_flag or False,
            nomination_target_flag=p.nomination_target_flag or False,
            value_assessment=p.value_assessment,
            adp_ai=float(p.adp_ai) if p.adp_ai is not None else None,
            # Per-format market ADP where a pipeline run has populated it; else the
            # players-table PPR value (adp_format_defaulted flags the fallback).
            adp_fantasypros=(
                ov.adp_fantasypros if ov.adp_fantasypros is not None
                else (float(p.adp_fantasypros) if p.adp_fantasypros is not None else None)
            ),
            adp_scoring=scoring_format if ov.adp_fantasypros is not None else p.adp_scoring,
            adp_rank=p.adp_rank,
            adp_diff=float(p.adp_diff) if p.adp_diff is not None else None,
            snake_flag=p.snake_flag,
            round_num=(p.adp_rank - 1) // _SNAKE_TEAM_COUNT + 1 if p.adp_rank else None,
            flags=flags,
            strategy_highlight=None,
        )

        # Apply strategy (auction only — snake has no strategy highlighting)
        if not is_snake and strategy in ("hero_rb", "zero_rb", "stars_and_scrubs", "balanced"):
            dbp.strategy_highlight = _apply_strategy(dbp, strategy)

        # Snake groups by round; auction groups by the (per-format) tier.
        group_key = str(dbp.round_num or 0) if is_snake else str(eff_tier or 0)
        if group_key not in tiers:
            tiers[group_key] = []
        tiers[group_key].append(dbp)
        total += 1

    return DraftBoardResponse(
        tiers=tiers,
        strategy=strategy,
        total_players=total,
        scoring_format=scoring_format,
        scoring_format_defaulted=fmt_defaulted,
        adp_format_defaulted=adp_format_defaulted,
    )
