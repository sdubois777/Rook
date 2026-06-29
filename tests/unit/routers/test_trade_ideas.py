"""
Tests for POST /api/trade/ideas (proposals — backend/routers/trade.py).

Pro-only gate order (403 before any deduct; 402 no-deduct; 20cr once), the
TRADE_DEMO_MODE bypass, the never-pad guarantee through the route, the empty
"no clear trade right now" result, the 5-cap, and proof a surfaced proposal's
verdict EQUALS what /api/trade/analyze returns for the same trade (same logic).
All CI-safe via overrides + a patched loader + fake (no-network) agents.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import backend.routers.trade as trade_mod
from backend.core.dependencies import get_credit_service, get_current_user, get_db
from backend.core.exceptions import InsufficientCreditsError
from backend.main import app
from backend.models.user import CREDIT_COSTS, User
from backend.routers.trade import get_trade_analyzer, get_trade_proposals_agent
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_proposals import Candidate
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _make_user(tier="pro", credits=50):
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.tier = tier
    u.credits_remaining = credits
    return u


class _FakeCredit:
    def __init__(self):
        self.deducts = []

    async def deduct(self, user, action, agent_name=None, cost_usd=None):
        cost = CREDIT_COSTS.get(action, 0)
        if user.credits_remaining < cost:
            raise InsufficientCreditsError(required=cost, available=user.credits_remaining)
        user.credits_remaining -= cost
        self.deducts.append((action, cost))
        return user.credits_remaining


def _iv(pid, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position="WR", forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="usage",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv / 5, expected_ppg=fv / 5, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv / 5, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _fixture(my, opp, roster_limit=16):
    me = TeamState("me", "Me", True, tuple(RosterPlayer(i, i.upper(), "WR") for i, _ in my))
    rivals = TeamState("opp", "Rivals", False, tuple(RosterPlayer(i, i.upper(), "WR") for i, _ in opp))
    state = LeagueState(2025, 14, (me, rivals))
    values = {i: _iv(i, fv) for i, fv in (*my, *opp)}
    return state, values, roster_limit


def _patch_loader(monkeypatch, league):
    async def _fake(db, user, demo):
        return league
    monkeypatch.setattr(trade_mod, "load_league_for_analysis", _fake)


def _fake_proposals(candidates):
    agent = MagicMock()
    agent.generate_candidates = AsyncMock(return_value=candidates)
    return agent


def _fake_analyzer():
    agent = MagicMock()
    agent.explain_trade = AsyncMock(return_value="grounded why")
    return agent


def _wire(user, candidates, credit=None):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_credit_service] = lambda: (credit or _FakeCredit())
    app.dependency_overrides[get_trade_proposals_agent] = lambda: _fake_proposals(candidates)
    app.dependency_overrides[get_trade_analyzer] = lambda: _fake_analyzer()


async def _post(path, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post(path, json=body)


# ---------------------------------------------------------------------------
# GATE ORDER (pro-only)
# ---------------------------------------------------------------------------
async def test_standard_user_403_zero_deduct(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="standard", credits=100)
    credit = _FakeCredit()
    _patch_loader(monkeypatch, _fixture([("g", 20)], [("x", 90)]))
    _wire(user, [Candidate(("g",), ("x",), "opp")], credit)
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403          # trade_finder is pro-only
    assert credit.deducts == []
    assert user.credits_remaining == 100


async def test_pro_insufficient_402_no_deduct(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="pro", credits=5)   # cost 20
    credit = _FakeCredit()
    _patch_loader(monkeypatch, _fixture([("g", 20)], [("x", 90)]))
    _wire(user, [Candidate(("g",), ("x",), "opp")], credit)
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 402
    assert credit.deducts == []
    assert user.credits_remaining == 5


async def test_pro_with_credits_runs_and_deducts_twenty_once(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="pro", credits=50)
    credit = _FakeCredit()
    _patch_loader(monkeypatch, _fixture([("g", 20)], [("x", 90)]))
    _wire(user, [Candidate(("g",), ("x",), "opp")], credit)
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert credit.deducts == [("trade_finder", 20)]
    assert user.credits_remaining == 30
    assert len(resp.json()["proposals"]) == 1


# ---------------------------------------------------------------------------
# DEMO BYPASS
# ---------------------------------------------------------------------------
async def test_demo_bypass_runs_without_gate(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="intro", credits=0)   # would 403 in prod
    credit = _FakeCredit()
    _patch_loader(monkeypatch, _fixture([("g", 20)], [("x", 90)]))
    _wire(user, [Candidate(("g",), ("x",), "opp")], credit)
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert credit.deducts == []
    assert resp.json()["demo_mode"] is True


# ---------------------------------------------------------------------------
# NEVER-PAD through the route
# ---------------------------------------------------------------------------
async def test_route_never_pads_when_only_one_candidate_is_good(monkeypatch):
    """The agent proposes five candidates (padding to a count would be tempting),
    but only one clears the verdict bar — the route returns exactly one."""
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    league = _fixture(
        [("g", 50)],
        [("x", 90), ("e1", 50), ("e2", 52), ("e3", 30), ("w1", 20)],
    )
    _patch_loader(monkeypatch, league)
    candidates = [Candidate(("g",), (o,), "opp") for o in ("x", "e1", "e2", "e3", "w1")]
    _wire(user, candidates)
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    data = resp.json()
    assert len(data["proposals"]) == 1                      # NOT padded to 3-5
    assert data["proposals"][0]["verdict"]["winner"] == "you"


async def test_route_empty_returns_no_clear_trade_message(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    league = _fixture([("g", 80)], [("a", 30), ("b", 40)])  # nothing gains me value
    _patch_loader(monkeypatch, league)
    _wire(user, [Candidate(("g",), ("a",), "opp"), Candidate(("g",), ("b",), "opp")])
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    data = resp.json()
    assert data["proposals"] == []
    assert data["message"] == "no clear trade right now"


async def test_route_caps_at_five(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    opps = [(f"o{i}", 90 - i) for i in range(8)]
    league = _fixture([("g", 10)], opps)
    _patch_loader(monkeypatch, league)
    _wire(user, [Candidate(("g",), (o,), "opp") for o, _ in opps])
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    assert len(resp.json()["proposals"]) == 5


# ---------------------------------------------------------------------------
# REUSE — a proposal's verdict equals /analyze for the same trade
# ---------------------------------------------------------------------------
async def test_proposal_verdict_equals_analyze_for_same_trade(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    league = _fixture([("g", 20)], [("x", 90)])
    _patch_loader(monkeypatch, league)
    _wire(user, [Candidate(("g",), ("x",), "opp")])
    try:
        ideas = (await _post("/api/trade/ideas", {})).json()
        verdict = ideas["proposals"][0]["verdict"]
        give = [p["id"] for p in verdict["give"]]
        get = [p["id"] for p in verdict["get"]]
        analyze = (await _post(
            "/api/trade/analyze", {"my_team_id": "me", "give": give, "get": get},
        )).json()
    finally:
        app.dependency_overrides.clear()

    for field in ("winner", "fairness", "value_delta", "give_value", "get_value",
                  "hedged", "confidence"):
        assert verdict[field] == analyze[field]   # identical verdict, same logic
