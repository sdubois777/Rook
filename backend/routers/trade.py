"""
Trade router — POST /api/trade/analyze (evaluate a trade the user builds).

Gating (uses the as-built machinery; NO config changes):
  - paid-only via the existing `trade_analyzer` feature + `trade_analysis` (10cr).
  - feature check (403) fires BEFORE any credit decrement; credits are deducted
    only on a call that actually runs the analysis (after input validation,
    before the agent). Intro stays locked out by design.
  - TRADE_DEMO_MODE is the ONLY bypass: when on, the route runs on the seeded
    demo league with no tier/credit gate (the demo league isn't a real tier).

The verdict is computed deterministically from engine value (trade_analysis.py);
the Sonnet agent only writes the rationale.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.core.dependencies import get_credit_service, get_current_user, get_db
from backend.services.trade.trade_analysis import (
    DEFAULT_ROSTER_LIMIT,
    TradeAnalysis,
    TradeValidationError,
    analyze_trade,
    validate_trade,
)
from backend.services.trade.trade_demo_source import trade_demo_enabled
from backend.services.trade.trade_proposals import evaluate_candidates

router = APIRouter(prefix="/trade", tags=["trade"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class TradeAnalyzeRequest(BaseModel):
    my_team_id: str
    give: list[str] = Field(default_factory=list, description="canonical player ids you send")
    get: list[str] = Field(default_factory=list, description="canonical player ids you receive")


class PlayerGroundingOut(BaseModel):
    id: str
    name: str
    position: str
    side: str
    forward_value: float
    value_trend: str
    confidence: str
    buy_low: bool
    sell_high: bool
    why: str


class RosterGuardOut(BaseModel):
    triggered: bool
    net_players: int
    open_slots: int
    drop_recommendations: list[dict]
    message: str


class TradeAnalyzeResponse(BaseModel):
    my_team_id: str
    winner: str
    fairness: str
    value_delta: float
    give_value: float
    get_value: float
    confidence: str
    hedged: bool
    hedge_reason: str
    give: list[PlayerGroundingOut]
    get: list[PlayerGroundingOut]
    roster_guard: RosterGuardOut
    rationale: str
    demo_mode: bool


class TradeIdeasRequest(BaseModel):
    my_team_id: Optional[str] = Field(
        default=None, description="defaults to your (is_me) team if omitted",
    )


class TradeIdea(BaseModel):
    counterparty_team_id: str
    counterparty_team_name: str
    why: str
    verdict: TradeAnalyzeResponse   # the full slice-3 verdict payload, unchanged


class TradeIdeasResponse(BaseModel):
    proposals: list[TradeIdea]      # 0-5; empty is a first-class result
    message: str                    # "" or "no clear trade right now"
    demo_mode: bool


# --- GET /trade/league (read-only roster + value exposer for the picker) -----
# TEST-ONLY demo surface (slice-6 teardown). Demo-aware via TRADE_DEMO_MODE.
class LeaguePlayerOut(BaseModel):
    id: str
    name: str
    position: str
    nfl_team: Optional[str] = None
    forward_value: float
    value_trend: str
    confidence: str
    buy_low: bool
    sell_high: bool


class LeagueTeamOut(BaseModel):
    team_id: str
    team_name: str
    is_me: bool
    roster: list[LeaguePlayerOut]


class TradeLeagueResponse(BaseModel):
    season: int
    week: int
    teams: list[LeagueTeamOut]
    demo_mode: bool


# ---------------------------------------------------------------------------
# Seams (patch points for tests + the slice-6 real provider)
# ---------------------------------------------------------------------------
async def load_league_for_analysis(db, user, demo: bool):
    """Return (LeagueState, {player_id: InSeasonValue}, roster_limit).

    Demo rides the existing TRADE_DEMO_MODE seam (#152). The real per-user
    league-state provider arrives with slice 6; until then the non-demo path
    raises 501 — AFTER the feature check and BEFORE any credit deduction, so a
    real user is never charged for an unavailable analysis."""
    if demo:
        from backend.services.trade.trade_demo_source import seed_demo_league
        from backend.services.trade.value_engine import evaluate_league

        source = await seed_demo_league(db)
        state = source.get_league_state()
        values = evaluate_league(state, source.weekly_usage, priors=source.priors)
        return state, values, DEFAULT_ROSTER_LIMIT

    raise HTTPException(
        status_code=501,
        detail="real-league trade analysis is not available yet (arrives with "
               "the real league-state provider); set TRADE_DEMO_MODE to try it.",
    )


def get_trade_analyzer():
    """Factory for the Sonnet rationale agent (overridable in tests)."""
    from backend.agents.trade_analyzer import TradeAnalyzerAgent
    return TradeAnalyzerAgent()


def get_trade_proposals_agent():
    """Factory for the Sonnet candidate-generation agent (overridable in tests)."""
    from backend.agents.trade_proposals import TradeProposalsAgent
    return TradeProposalsAgent()


def _to_response(a: TradeAnalysis, demo: bool) -> TradeAnalyzeResponse:
    def out(p):
        return PlayerGroundingOut(
            id=p.canonical_player_id, name=p.name, position=p.position, side=p.side,
            forward_value=p.forward_value, value_trend=p.value_trend,
            confidence=p.confidence, buy_low=p.buy_low, sell_high=p.sell_high, why=p.why,
        )
    return TradeAnalyzeResponse(
        my_team_id=a.my_team_id, winner=a.winner, fairness=a.fairness,
        value_delta=a.value_delta, give_value=a.give_value, get_value=a.get_value,
        confidence=a.confidence, hedged=a.hedged, hedge_reason=a.hedge_reason,
        give=[out(p) for p in a.give], get=[out(p) for p in a.get],
        roster_guard=RosterGuardOut(
            triggered=a.roster_guard.triggered, net_players=a.roster_guard.net_players,
            open_slots=a.roster_guard.open_slots,
            drop_recommendations=a.roster_guard.drop_recommendations,
            message=a.roster_guard.message,
        ),
        rationale=a.rationale, demo_mode=demo,
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@router.post("/analyze", response_model=TradeAnalyzeResponse)
async def analyze(
    body: TradeAnalyzeRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
    credit_service=Depends(get_credit_service),
    agent=Depends(get_trade_analyzer),
):
    demo = trade_demo_enabled()

    # 1. FEATURE GATE (403) — before anything else; skipped in demo.
    if not demo:
        from backend.services.feature_service import FeatureService
        FeatureService.check_feature_access(user, "trade_analyzer")

    # 2. Resolve the league + per-player engine values (501 if real not ready —
    #    still before any credit deduction).
    state, values, roster_limit = await load_league_for_analysis(db, user, demo)

    # 3. Validate the trade (400) — cheap, BEFORE any deduction.
    try:
        validate_trade(state, values, body.my_team_id, body.give, body.get)
    except TradeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 4. CREDIT DEDUCT (402) — only now, the analysis is about to run; skipped in demo.
    if not demo:
        await credit_service.deduct(user, "trade_analysis", agent_name="trade_analyzer")

    # 5. Deterministic verdict + Sonnet rationale.
    analysis = analyze_trade(
        state, values, body.my_team_id, body.give, body.get, roster_limit=roster_limit,
    )
    analysis.rationale = await agent.explain_trade(analysis)

    return _to_response(analysis, demo)


@router.post("/ideas", response_model=TradeIdeasResponse)
async def ideas(
    body: TradeIdeasRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
    credit_service=Depends(get_credit_service),
    proposals_agent=Depends(get_trade_proposals_agent),
    analyzer=Depends(get_trade_analyzer),
):
    """Pro-only: the system finds trades. Each surfaced idea is an agent-built
    trade run through slice-3's EXACT verdict path — never a second engine."""
    demo = trade_demo_enabled()

    # 1. FEATURE GATE (403, trade_finder = pro-only) — before anything; skipped in demo.
    if not demo:
        from backend.services.feature_service import FeatureService
        FeatureService.check_feature_access(user, "trade_finder")

    # 2. League + per-player values (501 if real not ready — before any deduct).
    state, values, roster_limit = await load_league_for_analysis(db, user, demo)

    my_team_id = body.my_team_id or (state.my_team.team_id if state.my_team else None)
    if my_team_id is None:
        raise HTTPException(status_code=400, detail="no team specified and no is_me team")

    # 3. CREDIT DEDUCT (402, 20cr) — only now, generation is about to run; skipped in demo.
    if not demo:
        await credit_service.deduct(user, "trade_finder", agent_name="trade_proposals")

    # 4. Generate candidates (LLM, deterministic fallback) → filter through the
    #    SAME slice-3 verdict + benefit bar → rank → cap (never-pad lives there).
    candidates = await proposals_agent.generate_candidates(state, my_team_id, values)
    surfaced = evaluate_candidates(
        state, values, my_team_id, candidates, roster_limit=roster_limit,
    )

    proposals: list[TradeIdea] = []
    for cand, analysis in surfaced:
        analysis.rationale = await analyzer.explain_trade(analysis)
        team_name = next(
            (t.team_name for t in state.teams if t.team_id == cand.counterparty_team_id),
            cand.counterparty_team_id,
        )
        proposals.append(TradeIdea(
            counterparty_team_id=cand.counterparty_team_id,
            counterparty_team_name=team_name,
            why=analysis.rationale,
            verdict=_to_response(analysis, demo),
        ))

    message = "" if proposals else "no clear trade right now"
    return TradeIdeasResponse(proposals=proposals, message=message, demo_mode=demo)


@router.get("/league", response_model=TradeLeagueResponse)
async def league(user=Depends(get_current_user), db=Depends(get_db)):
    """READ-ONLY support for the trade page's picker + team-switcher. Exposes the
    SAME slice-2 seeded demo LeagueState run through the SAME evaluate_league, so
    picker values match verdict values exactly. Demo-only: with TRADE_DEMO_MODE
    off it 404s (no real-league exposure here). Adds NO trade logic — it just
    reshapes what load_league_for_analysis already returns."""
    demo = trade_demo_enabled()
    if not demo:
        raise HTTPException(
            status_code=404,
            detail="trade demo league is only available under TRADE_DEMO_MODE",
        )

    state, values, _ = await load_league_for_analysis(db, user, demo)

    teams: list[LeagueTeamOut] = []
    for team in state.teams:
        roster: list[LeaguePlayerOut] = []
        for rp in team.roster:
            v = values.get(rp.canonical_player_id)
            roster.append(LeaguePlayerOut(
                id=rp.canonical_player_id, name=rp.name, position=rp.position,
                nfl_team=rp.nfl_team,
                forward_value=v.forward_value if v else 0.0,
                value_trend=v.value_trend.value if v else "stable",
                confidence=v.confidence.value if v else "insufficient",
                buy_low=v.buy_low if v else False,
                sell_high=v.sell_high if v else False,
            ))
        teams.append(LeagueTeamOut(
            team_id=team.team_id, team_name=team.team_name,
            is_me=team.is_me, roster=roster,
        ))

    return TradeLeagueResponse(
        season=state.season, week=state.week, teams=teams, demo_mode=demo,
    )
