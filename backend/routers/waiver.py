"""
Waiver router — GET /api/waiver/league (demo picker) + POST /api/waiver/recommendations.

Mirrors the trade router exactly:
  - paid-only via the existing ``waiver_wire`` feature + credit (8cr); the feature
    check (403) fires BEFORE any credit decrement.
  - WAIVER_DEMO_MODE is the only bypass; with it off, /league 404s and
    /recommendations 501s (the real per-league provider is a follow-up), both
    BEFORE any charge.

A waiver add/drop is a one-sided trade — recommendations compose the pure trade
lineup objective (services/trade/lineup.py); this router adds NO new value math.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.dependencies import get_credit_service, get_current_user, get_db
from backend.services.trade.trade_analysis import DEFAULT_ROSTER_LIMIT
from backend.services.waiver.news_tiein import build_news_map
from backend.services.waiver.recommendations import Recommendation, best_add, recommend
from backend.services.waiver.waiver_demo_source import (
    seed_demo_waiver,
    waiver_demo_enabled,
    waiver_demo_enforce_gates,
)

router = APIRouter(prefix="/waiver", tags=["waiver"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class WaiverRecommendationsRequest(BaseModel):
    my_team_id: Optional[str] = Field(None, description="acting team; defaults to your team")


class LeaguePlayerOut(BaseModel):
    id: str
    name: str
    position: str
    nfl_team: Optional[str]
    starter_slot: Optional[str]
    forward_value: float
    value_trend: str
    confidence: str
    injury_status: Optional[str] = None   # badge code "Q"|"D"|"O"|"IR"; None = healthy


class LeagueTeamOut(BaseModel):
    team_id: str
    team_name: str
    is_me: bool
    faab_remaining: int
    roster: list[LeaguePlayerOut]


class WaiverSettingsOut(BaseModel):
    type: str
    budget: int
    remaining: int


class WaiverLeagueResponse(BaseModel):
    season: int
    week: int
    teams: list[LeagueTeamOut]
    waiver_type: str
    faab_budget: int
    demo_mode: bool
    enforced: bool


class AddOut(BaseModel):
    id: str
    name: str
    position: str
    nfl_team: Optional[str]
    forward_value: float
    forward_ppg: float
    value_trend: str
    confidence: str
    buy_low: bool
    sell_high: bool
    injury_status: Optional[str] = None   # badge code "Q"|"D"|"O"|"IR"; None = healthy


class DropOut(BaseModel):
    id: str
    name: str
    position: str
    forward_value: float
    injury_status: Optional[str] = None   # badge code "Q"|"D"|"O"|"IR"; None = healthy


class FaabOut(BaseModel):
    recommended: bool
    tier_label: str
    total_bid: int
    base_bid: int
    news_bump_bid: int
    pct_of_remaining: float
    why: str


class NewsOut(BaseModel):
    kind: str
    headline: str
    signal_type: str
    confidence: Optional[str]
    source: Optional[str]
    flagged_at: Optional[str]
    starter_name: Optional[str] = None
    contingent_impact_pct: Optional[float] = None
    contingent_reasoning: Optional[str] = None


class MatchupOut(BaseModel):
    """DST matchup context for the demo week (slice 4 tilt, slice 5a display). Additive;
    present only on DST recs that were tilted. tilt_ppw is the honest small delta the
    projection applied (~±2.5 cap) — the frontend renders restrained copy from it."""
    opponent: Optional[str] = None
    tilt_ppw: float


class RecommendationOut(BaseModel):
    add: AddOut
    drop: Optional[DropOut]
    lineup_delta_ppw: float
    fills_need: bool
    need_positions: list[str]
    faab: FaabOut
    news: Optional[NewsOut]
    matchup: Optional[MatchupOut] = None
    why: str


class SilenceOut(BaseModel):
    reason: str
    near_miss_name: Optional[str] = None
    near_miss_gain: Optional[float] = None


class WaiverRecommendationsResponse(BaseModel):
    season: int
    week: int
    my_team_id: str
    my_team_name: str
    waiver: WaiverSettingsOut
    needs: list[str]
    recommendations: list[RecommendationOut]
    silence: Optional[SilenceOut]
    demo_mode: bool
    enforced: bool


# ---------------------------------------------------------------------------
# Seam
# ---------------------------------------------------------------------------
async def load_waiver_source(db, demo: bool):
    """Demo rides WAIVER_DEMO_MODE; the real per-league provider is a follow-up →
    501 until then (after the feature check, before any credit deduction)."""
    if demo:
        return await seed_demo_waiver(db)
    raise HTTPException(
        status_code=501,
        detail="real-league waiver recommendations are not available yet; "
               "set WAIVER_DEMO_MODE to try it.",
    )


def _rec_out(
    r: Recommendation,
    dst_matchup: dict[str, dict] | None = None,
    injury_by_id: dict[str, str] | None = None,
) -> RecommendationOut:
    v = r.add
    mc = (dst_matchup or {}).get(v.canonical_player_id) if v.position == "DEF" else None
    inj = injury_by_id or {}
    return RecommendationOut(
        add=AddOut(
            id=v.canonical_player_id, name=v.name, position=v.position,
            nfl_team=r.add_nfl_team, forward_value=v.forward_value, forward_ppg=v.forward_ppg,
            value_trend=v.value_trend.value, confidence=v.confidence.value,
            buy_low=v.buy_low, sell_high=v.sell_high,
            injury_status=inj.get(v.canonical_player_id),
        ),
        drop=DropOut(id=r.drop.id, name=r.drop.name, position=r.drop.position,
                     forward_value=r.drop.forward_value,
                     injury_status=inj.get(r.drop.id)) if r.drop else None,
        lineup_delta_ppw=r.lineup_delta_ppw, fills_need=r.fills_need,
        need_positions=list(r.need_positions),
        faab=FaabOut(
            recommended=r.faab.recommended, tier_label=r.faab.tier_label,
            total_bid=r.faab.total_bid, base_bid=r.faab.base_bid,
            news_bump_bid=r.faab.news_bump_bid, pct_of_remaining=r.faab.pct_of_remaining,
            why=r.faab.why,
        ),
        news=NewsOut(
            kind=r.news.kind, headline=r.news.headline, signal_type=r.news.signal_type,
            confidence=r.news.confidence, source=r.news.source, flagged_at=r.news.flagged_at,
            starter_name=r.news.starter_name, contingent_impact_pct=r.news.contingent_impact_pct,
            contingent_reasoning=r.news.contingent_reasoning,
        ) if r.news else None,
        matchup=MatchupOut(opponent=mc.get("opponent"), tilt_ppw=mc.get("tilt", 0.0)) if mc else None,
        why=r.why,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/league", response_model=WaiverLeagueResponse)
async def league(user=Depends(get_current_user), db=Depends(get_db)):
    """READ-ONLY picker/team-switcher for the waiver page. Demo-only: 404s with
    WAIVER_DEMO_MODE off (no real-league exposure here)."""
    demo = waiver_demo_enabled()
    if not demo:
        raise HTTPException(
            status_code=404,
            detail="waiver demo league is only available under WAIVER_DEMO_MODE",
        )
    src = await load_waiver_source(db, demo)

    teams: list[LeagueTeamOut] = []
    for team in src.state.teams:
        roster: list[LeaguePlayerOut] = []
        for rp in team.roster:
            v = src.values.get(rp.canonical_player_id)
            roster.append(LeaguePlayerOut(
                id=rp.canonical_player_id, name=rp.name, position=rp.position,
                nfl_team=rp.nfl_team, starter_slot=rp.starter_slot,
                forward_value=v.forward_value if v else 0.0,
                value_trend=v.value_trend.value if v else "stable",
                confidence=v.confidence.value if v else "insufficient",
                injury_status=rp.injury_status,
            ))
        teams.append(LeagueTeamOut(
            team_id=team.team_id, team_name=team.team_name, is_me=team.is_me,
            faab_remaining=src.faab_remaining_by_team.get(team.team_id, src.faab_budget),
            roster=roster,
        ))

    return WaiverLeagueResponse(
        season=src.state.season, week=src.state.week, teams=teams,
        waiver_type=src.waiver_type, faab_budget=src.faab_budget,
        demo_mode=demo, enforced=waiver_demo_enforce_gates(),
    )


@router.post("/recommendations", response_model=WaiverRecommendationsResponse)
async def recommendations(
    body: WaiverRecommendationsRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
    credit_service=Depends(get_credit_service),
):
    demo = waiver_demo_enabled()
    enforce = (not demo) or waiver_demo_enforce_gates()

    # 1. FEATURE GATE (403) — before anything else.
    if enforce:
        from backend.services.feature_service import FeatureService
        FeatureService.check_feature_access(user, "waiver_wire")

    # 2. Resolve the demo source (501 if real not ready — still before any charge).
    src = await load_waiver_source(db, demo)

    # 3. Resolve the acting team.
    acting = None
    if body.my_team_id:
        acting = next((t for t in src.state.teams if t.team_id == body.my_team_id), None)
        if acting is None:
            raise HTTPException(status_code=400, detail=f"team {body.my_team_id!r} not in league")
    acting = acting or src.state.my_team or (src.state.teams[0] if src.state.teams else None)
    if acting is None:
        raise HTTPException(status_code=400, detail="no team to recommend for")

    # 4. CREDIT DEDUCT (402) — only now, the recommendation is about to run.
    if enforce:
        await credit_service.deduct(user, "waiver_wire", agent_name="waiver_wire")

    # 5. News tie-in (depth-chart backbone + CONTINGENT enrichment), then rank.
    pool_ids = {rp.canonical_player_id for rp in src.pool}
    news_map = await build_news_map(db, pool_ids, now=datetime.now(timezone.utc))
    faab_remaining = src.faab_remaining_by_team.get(acting.team_id, src.faab_budget)

    recs = recommend(
        acting, src.pool, src.values,
        roster_limit=DEFAULT_ROSTER_LIMIT, faab_remaining=faab_remaining, news_map=news_map,
    )

    from backend.services.trade.trade_proposals import analyze_roster
    needs = sorted(analyze_roster(acting, src.values).needs)

    silence = None
    if not recs:
        nm = best_add(acting, src.pool, src.values, roster_limit=DEFAULT_ROSTER_LIMIT)
        silence = SilenceOut(
            reason="Nothing on waivers cracks your starting lineup right now.",
            near_miss_name=nm[0].name if nm else None,
            near_miss_gain=round(nm[1], 2) if nm else None,
        )

    # Injury badge — id → canonical status across the pool + every rostered player
    # (the add comes from the pool, the drop from the acting roster).
    injury_by_id = {
        rp.canonical_player_id: rp.injury_status
        for rp in [*src.pool, *(p for t in src.state.teams for p in t.roster)]
        if rp.injury_status
    }

    return WaiverRecommendationsResponse(
        season=src.state.season, week=src.state.week,
        my_team_id=acting.team_id, my_team_name=acting.team_name,
        waiver=WaiverSettingsOut(type=src.waiver_type, budget=src.faab_budget, remaining=faab_remaining),
        needs=needs,
        recommendations=[_rec_out(r, getattr(src, "dst_matchup", {}), injury_by_id) for r in recs],
        silence=silence,
        demo_mode=demo, enforced=waiver_demo_enforce_gates(),
    )
