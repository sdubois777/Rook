"""
Players router — browsing, searching, and detailed player views.

Endpoints:
  GET /players          — paginated, filterable player list
  GET /players/search   — name search (debounced from frontend)
  GET /players/summary  — position counts for scarcity display
  GET /players/{id}     — full player detail with all related data
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from backend.core.dependencies import get_current_user, get_db
from backend.engines.valuation import get_market_context
from backend.utils.seasons import get_current_season
from backend.models.player import Player
from backend.repositories.player_repo import PlayerRepository
from backend.repositories.team_system_repo import TeamSystemRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/players", tags=["players"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class FlagSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    flag_type: str
    trigger_player_id: Optional[str] = None
    trigger_player_name: Optional[str] = None
    trigger_condition: Optional[str] = None
    effect_on_value: Optional[str] = None
    value_impact_pct: Optional[float] = None
    confidence: Optional[str] = None
    reasoning: Optional[str] = None


class PlayerSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    team_abbr: Optional[str] = None
    position: Optional[str] = None
    age: Optional[int] = None
    tier: Optional[int] = None
    recommended_bid_ceiling: Optional[float] = None
    baseline_value: Optional[float] = None
    ceiling_value: Optional[float] = None
    floor_value: Optional[float] = None
    market_value: Optional[float] = None
    market_value_season: Optional[int] = None
    prior_season_price: Optional[float] = None
    prior_season_year: Optional[int] = None
    value_gap: Optional[float] = None
    value_gap_signal: Optional[str] = None
    situation_score: Optional[str] = None
    breakout_flag: bool = False
    is_rookie: bool = False
    notes: Optional[str] = None
    flags: list[FlagSummary] = []
    injury_risk_level: Optional[str] = None
    schedule_score: Optional[float] = None
    ai_bid_ceiling: Optional[int] = None
    pay_up_flag: bool = False
    nomination_target_flag: bool = False


class PlayerListResponse(BaseModel):
    players: list[PlayerSummary]
    total: int
    page: int
    per_page: int
    pages: int


class ProfileDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role_classification: Optional[str] = None
    target_share_3yr_avg: Optional[float] = None
    target_share_last_season: Optional[float] = None
    targets_per_route_run: Optional[float] = None
    air_yards_share: Optional[float] = None
    snap_percentage: Optional[float] = None
    efficiency_signal: Optional[str] = None
    age_curve_position: Optional[str] = None
    clean_season_baseline: Optional[dict] = None
    breakout_flag: bool = False
    breakout_reasoning: Optional[str] = None
    projection_reasoning: Optional[str] = None
    positional_scarcity_tier: Optional[str] = None
    career_trajectory: Optional[str] = None
    confidence: Optional[str] = None
    separation_score: Optional[str] = None
    yards_after_catch_score: Optional[str] = None
    is_rookie: bool = False
    profile_source: Optional[str] = None
    ceiling_value_ppr: Optional[float] = None
    floor_value_ppr: Optional[float] = None
    variance_flag: bool = False
    breakout_window: Optional[str] = None
    year1_role: Optional[str] = None


class InjuryDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    overall_risk_level: Optional[str] = None
    risk_adjusted_value_modifier: Optional[float] = None
    injury_log: Optional[list] = None
    pattern_flags: Optional[list] = None
    workload_cliff_flag: bool = False
    high_mileage_flag: bool = False
    post_acl_flag: bool = False
    concussion_count: int = 0
    career_carry_count: Optional[int] = None
    recovery_assessment: Optional[str] = None
    risk_notes: Optional[str] = None


class ScheduleDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bye_week: Optional[int] = None
    bye_in_playoff_window: bool = False
    early_window_grade: Optional[str] = None
    full_season_grade: Optional[str] = None
    playoff_window_grade: Optional[str] = None
    playoff_weeks: Optional[list] = None
    playoff_summary: Optional[str] = None
    schedule_score: Optional[float] = None
    schedule_notes: Optional[str] = None


class SignalDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    signal_type: str
    source: Optional[str] = None
    raw_text: Optional[str] = None
    confidence: Optional[str] = None
    flagged_at: Optional[str] = None


class TeamSystemSummary(BaseModel):
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


class PlayerDetail(PlayerSummary):
    profile: Optional[ProfileDetail] = None
    injury_profile: Optional[InjuryDetail] = None
    schedule: Optional[ScheduleDetail] = None
    dependencies: list[FlagSummary] = []
    beat_signals: list[SignalDetail] = []
    team_system: Optional[TeamSystemSummary] = None
    ai_confidence_floor: Optional[int] = None
    ai_confidence_ceiling: Optional[int] = None
    value_assessment: Optional[str] = None
    auction_note: Optional[str] = None
    league_bias: Optional[float] = None
    league_bias_signal: Optional[str] = None


class PositionCounts(BaseModel):
    tier1: int = 0
    tier2: int = 0
    tier3: int = 0
    total: int = 0


class PlayerSummaryResponse(BaseModel):
    position_counts: dict[str, PositionCounts]
    total_players: int


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_prior_season_price(player: Player) -> tuple[float | None, int | None]:
    """Look up prior season price from historic_prices relationship."""
    prior_year = get_current_season() - 1
    for hp in (player.historic_prices or []):
        if hp.season_year == prior_year:
            return float(hp.price), prior_year
    return None, None


def _player_to_summary(player: Player) -> PlayerSummary:
    """Convert a Player ORM object to PlayerSummary response."""
    flags = []
    for dep in (player.dependencies or []):
        flags.append(FlagSummary(
            id=str(dep.id),
            flag_type=dep.flag_type,
            trigger_player_id=str(dep.trigger_player_id) if dep.trigger_player_id else None,
            trigger_player_name=dep.trigger_player_name,
            trigger_condition=dep.trigger_condition,
            effect_on_value=dep.effect_on_value,
            value_impact_pct=float(dep.value_impact_pct) if dep.value_impact_pct else None,
            confidence=dep.confidence,
            reasoning=dep.reasoning,
        ))

    prior_price, prior_year = _get_prior_season_price(player)

    return PlayerSummary(
        id=str(player.id),
        name=player.name,
        team_abbr=player.team_abbr,
        position=player.position,
        age=player.age,
        tier=player.tier,
        recommended_bid_ceiling=float(player.recommended_bid_ceiling) if player.recommended_bid_ceiling else None,
        baseline_value=float(player.baseline_value) if player.baseline_value else None,
        ceiling_value=float(player.ceiling_value) if player.ceiling_value else None,
        floor_value=float(player.floor_value) if player.floor_value else None,
        market_value=float(player.market_value_fantasypros) if player.market_value_fantasypros else None,
        market_value_season=get_current_season() if player.market_value_fantasypros else None,
        prior_season_price=prior_price,
        prior_season_year=prior_year,
        value_gap=float(player.value_gap) if player.value_gap else None,
        value_gap_signal=player.value_gap_signal,
        situation_score=player.situation_score,
        breakout_flag=player.breakout_flag or False,
        is_rookie=player.is_rookie or False,
        notes=player.notes,
        flags=flags,
        injury_risk_level=player.injury_profile.overall_risk_level if player.injury_profile else None,
        schedule_score=float(player.schedule.schedule_score) if player.schedule and player.schedule.schedule_score else None,
        ai_bid_ceiling=player.ai_bid_ceiling,
        pay_up_flag=player.pay_up_flag or False,
        nomination_target_flag=player.nomination_target_flag or False,
    )


# ---------------------------------------------------------------------------
# Endpoints — search and summary MUST be before /{id}
# ---------------------------------------------------------------------------

@router.get("/search", response_model=list[PlayerSummary])
async def search_players(
    q: str = Query(..., min_length=2),
    db=Depends(get_db),
) -> list[PlayerSummary]:
    """Search players by name. Returns top 20 matches by bid ceiling."""
    repo = PlayerRepository(db)
    players = await repo.search_by_name(q, limit=20)
    return [_player_to_summary(p) for p in players]


@router.get("/summary", response_model=PlayerSummaryResponse)
async def player_summary(db=Depends(get_db)) -> PlayerSummaryResponse:
    """Position counts by tier for scarcity display."""
    repo = PlayerRepository(db)
    rows = await repo.count_by_position_tier()
    total_players = await repo.count_all()

    position_counts: dict[str, PositionCounts] = {}
    for pos, tier, count in rows:
        if pos not in position_counts:
            position_counts[pos] = PositionCounts()
        pc = position_counts[pos]
        pc.total += count
        if tier == 1:
            pc.tier1 = count
        elif tier == 2:
            pc.tier2 = count
        elif tier == 3:
            pc.tier3 = count

    return PlayerSummaryResponse(
        position_counts=position_counts,
        total_players=total_players,
    )


@router.get("/{player_id}", response_model=PlayerDetail)
async def get_player(player_id: uuid.UUID, db=Depends(get_db)) -> PlayerDetail:
    """Full player detail with all related data."""
    repo = PlayerRepository(db)
    player = await repo.get_detail(player_id)

    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # Get team system context
    team_system = None
    if player.team_abbr:
        ts = await TeamSystemRepository(db).get_latest_for_team(player.team_abbr)
        if ts:
            team_system = TeamSystemSummary(
                team_abbr=ts.team_abbr,
                system_grade=ts.system_grade,
                qb_name=ts.qb_name,
                qb_tier=ts.qb_tier,
                pass_protection_grade=ts.pass_protection_grade,
                run_blocking_grade=ts.run_blocking_grade,
                oc_scheme=ts.oc_scheme,
                rookie_qb_flag=ts.rookie_qb_flag or False,
                compound_risk_flag=ts.compound_risk_flag or False,
            )

    summary = _player_to_summary(player)

    # Build detail sections
    profile = None
    if player.profile:
        p = player.profile
        profile = ProfileDetail(
            role_classification=p.role_classification,
            target_share_3yr_avg=float(p.target_share_3yr_avg) if p.target_share_3yr_avg else None,
            target_share_last_season=float(p.target_share_last_season) if p.target_share_last_season else None,
            targets_per_route_run=float(p.targets_per_route_run) if p.targets_per_route_run else None,
            air_yards_share=float(p.air_yards_share) if p.air_yards_share else None,
            snap_percentage=float(p.snap_percentage) if p.snap_percentage else None,
            efficiency_signal=p.efficiency_signal,
            age_curve_position=p.age_curve_position,
            clean_season_baseline=p.clean_season_baseline,
            breakout_flag=p.breakout_flag or False,
            breakout_reasoning=p.breakout_reasoning,
            projection_reasoning=p.projection_reasoning,
            positional_scarcity_tier=p.positional_scarcity_tier,
            career_trajectory=p.career_trajectory,
            confidence=p.confidence,
            separation_score=p.separation_score,
            yards_after_catch_score=p.yards_after_catch_score,
            is_rookie=p.is_rookie or False,
            profile_source=p.profile_source,
            ceiling_value_ppr=float(p.ceiling_value_ppr) if p.ceiling_value_ppr else None,
            floor_value_ppr=float(p.floor_value_ppr) if p.floor_value_ppr else None,
            variance_flag=p.variance_flag or False,
            breakout_window=p.breakout_window,
            year1_role=p.year1_role,
        )

    injury_profile = None
    if player.injury_profile:
        ip = player.injury_profile
        injury_profile = InjuryDetail(
            overall_risk_level=ip.overall_risk_level,
            risk_adjusted_value_modifier=float(ip.risk_adjusted_value_modifier) if ip.risk_adjusted_value_modifier else None,
            injury_log=ip.injury_log,
            pattern_flags=ip.pattern_flags,
            workload_cliff_flag=ip.workload_cliff_flag or False,
            high_mileage_flag=ip.high_mileage_flag or False,
            post_acl_flag=ip.post_acl_flag or False,
            concussion_count=ip.concussion_count or 0,
            career_carry_count=ip.career_carry_count,
            recovery_assessment=ip.recovery_assessment,
            risk_notes=ip.risk_notes,
        )

    schedule = None
    if player.schedule:
        s = player.schedule
        schedule = ScheduleDetail(
            bye_week=s.bye_week,
            bye_in_playoff_window=s.bye_in_playoff_window or False,
            early_window_grade=s.early_window_grade,
            full_season_grade=s.full_season_grade,
            playoff_window_grade=s.playoff_window_grade,
            playoff_weeks=s.playoff_weeks,
            playoff_summary=s.playoff_summary,
            schedule_score=float(s.schedule_score) if s.schedule_score else None,
            schedule_notes=s.schedule_notes,
        )

    beat_signals = []
    for sig in (player.beat_signals or [])[:10]:
        beat_signals.append(SignalDetail(
            id=str(sig.id),
            signal_type=sig.signal_type,
            source=sig.source,
            raw_text=sig.raw_text,
            confidence=sig.confidence,
            flagged_at=sig.flagged_at.isoformat() if sig.flagged_at else None,
        ))

    mctx = get_market_context(player)

    return PlayerDetail(
        **summary.model_dump(),
        profile=profile,
        injury_profile=injury_profile,
        schedule=schedule,
        dependencies=summary.flags,
        beat_signals=beat_signals,
        team_system=team_system,
        ai_confidence_floor=player.ai_confidence_floor,
        ai_confidence_ceiling=player.ai_confidence_ceiling,
        value_assessment=player.value_assessment,
        auction_note=player.auction_note,
        league_bias=float(mctx["league_bias"]) if mctx["league_bias"] is not None else None,
        league_bias_signal=mctx["league_bias_signal"],
    )


@router.get("", response_model=PlayerListResponse)
async def list_players(
    position: Optional[str] = None,
    tier: Optional[int] = None,
    team: Optional[str] = None,
    flag: Optional[str] = None,
    value_gap_dir: Optional[str] = None,
    sort: str = "bid_ceiling",
    order: str = "desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    _user=Depends(get_current_user),
    db=Depends(get_db),
) -> PlayerListResponse:
    """Paginated, filterable player list."""
    repo = PlayerRepository(db)
    players, total = await repo.list_filtered(
        position=position,
        tier=tier,
        team=team,
        flag=flag,
        value_gap_dir=value_gap_dir,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
    )

    pages = (total + per_page - 1) // per_page if total > 0 else 1

    return PlayerListResponse(
        players=[_player_to_summary(p) for p in players],
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
    )
