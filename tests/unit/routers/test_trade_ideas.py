"""
Tests for POST /api/trade/ideas (proposals — backend/routers/trade.py).

Pro-only gate order (403 before any deduct; 402 no-deduct; 20cr once), the
TRADE_DEMO_MODE bypass, the edge-band surfacing through the route (incl. the
edge payload), the never-pad empty result, and proof a surfaced proposal's
verdict EQUALS what /api/trade/analyze returns for the same trade. CI-safe via
overrides + a patched loader + fake (no-network) agents.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

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

# A real positive-sum league: me RB-rich/WR-thin, them WR-rich/RB-thin.
# rm4(surplus RB) ↔ wt5(surplus WR) clears the edge band; wt1-wt4 are their
# starters (giving them fails the opponent-comfort condition).
ME = [("qm", "QB", 22), ("rm1", "RB", 24), ("rm2", "RB", 22), ("rm3", "RB", 20),
      ("rm4", "RB", 15), ("wm1", "WR", 16), ("wm2", "WR", 14), ("tm", "TE", 15)]
THEM = [("qt", "QB", 19), ("rt1", "RB", 9), ("btr", "RB", 7),
        ("wt1", "WR", 20), ("wt2", "WR", 18), ("wt3", "WR", 16), ("wt4", "WR", 14),
        ("wt5", "WR", 13), ("tt", "TE", 14)]
# Strictly-dominant me — no swap helps both sides (never-pad).
DOM_ME = [("q", "QB", 25), ("r1", "RB", 24), ("r2", "RB", 22), ("w1", "WR", 20),
          ("w2", "WR", 18), ("w3", "WR", 16), ("t", "TE", 14), ("br", "RB", 13)]
WEAK_THEM = [("oq", "QB", 10), ("or1", "RB", 9), ("or2", "RB", 7), ("ow1", "WR", 8),
             ("ow2", "WR", 6), ("ow3", "WR", 5), ("ot", "TE", 4)]


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


def _iv(pid, pos, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="usage",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _fixture(my, opp, roster_limit=16):
    me = TeamState("me", "Me", True, tuple(RosterPlayer(i, i.upper(), pos) for i, pos, _ in my))
    rivals = TeamState("opp", "Rivals", False, tuple(RosterPlayer(i, i.upper(), pos) for i, pos, _ in opp))
    state = LeagueState(2025, 14, (me, rivals))
    values = {i: _iv(i, pos, fv) for i, pos, fv in (*my, *opp)}
    return state, values, roster_limit


def _patch_loader(monkeypatch, league):
    async def _fake(db, user, demo):
        return league
    monkeypatch.setattr(trade_mod, "load_league_for_analysis", _fake)


def _wire(user, candidates, credit=None):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_credit_service] = lambda: (credit or _FakeCredit())
    agent = MagicMock()
    agent.generate_candidates = AsyncMock(return_value=candidates)
    app.dependency_overrides[get_trade_proposals_agent] = lambda: agent
    analyzer = MagicMock()
    analyzer.explain_trade = AsyncMock(return_value="grounded why")
    app.dependency_overrides[get_trade_analyzer] = lambda: analyzer


async def _post(path, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post(path, json=body)


_CLEARING = [Candidate(("rm4",), ("wt5",), "opp")]


# ---------------------------------------------------------------------------
# GATE ORDER (pro-only) — fires before generation, so surfacing is irrelevant
# ---------------------------------------------------------------------------
async def test_standard_user_403_zero_deduct(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="standard", credits=100)
    credit = _FakeCredit()
    _patch_loader(monkeypatch, _fixture(ME, THEM))
    _wire(user, _CLEARING, credit)
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
    _patch_loader(monkeypatch, _fixture(ME, THEM))
    _wire(user, _CLEARING, credit)
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
    _patch_loader(monkeypatch, _fixture(ME, THEM))
    _wire(user, _CLEARING, credit)
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert credit.deducts == [("trade_finder", 20)]
    assert user.credits_remaining == 30
    data = resp.json()
    assert len(data["proposals"]) == 1
    edge = data["proposals"][0]["edge"]            # the new edge payload is surfaced
    assert edge["your_lineup_gain"] > 0 and edge["their_lineup_gain"] > 0
    assert edge["my_strength"] >= edge["their_strength"]


# ---------------------------------------------------------------------------
# DEMO BYPASS
# ---------------------------------------------------------------------------
async def test_demo_bypass_runs_without_gate(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="intro", credits=0)   # would 403 in prod
    credit = _FakeCredit()
    _patch_loader(monkeypatch, _fixture(ME, THEM))
    _wire(user, _CLEARING, credit)
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
async def test_route_never_pads_to_five_when_fewer_clear(monkeypatch):
    """The agent proposes five candidates (my surplus RB for each of their WRs).
    Only the swaps that IMPROVE BOTH lineups clear (giving rm4 for their better WRs
    helps me, but giving them rm4 only improves their RB-thin lineup when what they
    part with isn't a top starter) — fewer than five surface, NOT a padded list."""
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    _patch_loader(monkeypatch, _fixture(ME, THEM))
    cands = [Candidate(("rm4",), (w,), "opp") for w in ("wt5", "wt4", "wt3", "wt2", "wt1")]
    _wire(user, cands)
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    data = resp.json()
    assert 0 < len(data["proposals"]) < 5         # some clear, but NOT padded to 5
    assert all(p["counterparty_team_name"] == "Rivals" for p in data["proposals"])


async def test_route_empty_returns_no_clear_trade_message(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    _patch_loader(monkeypatch, _fixture(DOM_ME, WEAK_THEM))   # strictly dominant me
    _wire(user, [Candidate(("r1",), ("ow1",), "opp"), Candidate(("r2",), ("ow2",), "opp")])
    try:
        resp = await _post("/api/trade/ideas", {})
    finally:
        app.dependency_overrides.clear()
    data = resp.json()
    assert data["proposals"] == []
    assert data["message"] == "no clear trade right now"


# ---------------------------------------------------------------------------
# REUSE — a proposal's verdict equals /analyze for the same trade
# ---------------------------------------------------------------------------
async def test_proposal_verdict_equals_analyze_for_same_trade(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    _patch_loader(monkeypatch, _fixture(ME, THEM))
    _wire(user, _CLEARING)
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
