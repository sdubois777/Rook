"""
Tests for POST /api/trade/analyze (backend/routers/trade.py).

Covers the gate ORDER (feature 403 before any credit deduct; 402 on insufficient
with no deduction; a paid call deducts exactly 10cr once), the TRADE_DEMO_MODE
bypass, the roster guard, and request validation — all CI-safe via dependency
overrides + a patched league loader + a fake (no-network) rationale agent. A
guarded test exercises the real seeded demo league end-to-end.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import backend.routers.trade as trade_mod
from backend.core.dependencies import (
    get_credit_service,
    get_current_user,
    get_db,
)
from backend.main import app
from backend.models.user import User
from backend.core.exceptions import InsufficientCreditsError
from backend.models.user import CREDIT_COSTS
from backend.routers.trade import get_trade_analyzer
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


# ---------------------------------------------------------------------------
# fixtures / fakes
# ---------------------------------------------------------------------------
def _make_user(tier="standard", credits=50):
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


def _iv(pid, name, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=name, position="WR", forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="usage",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _fixture_league(roster_limit=16):
    me = TeamState("me", "Me", True, (RosterPlayer("g", "Give", "WR"), RosterPlayer("b", "Bench", "WR")))
    opp = TeamState("opp", "Opp", False, (RosterPlayer("x", "Get", "WR"), RosterPlayer("y", "Get2", "WR")))
    state = LeagueState(2025, 14, (me, opp))
    values = {"g": _iv("g", "Give", 20), "b": _iv("b", "Bench", 5),
              "x": _iv("x", "Get", 90), "y": _iv("y", "Get2", 30)}
    return state, values, roster_limit


def _patch_loader(monkeypatch, league=None):
    league = league or _fixture_league()

    async def _fake(db, user, demo):
        return league

    monkeypatch.setattr(trade_mod, "load_league_for_analysis", _fake)


def _fake_agent():
    agent = MagicMock()
    agent.explain_trade = AsyncMock(return_value="grounded rationale")
    return agent


async def _post(body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.post("/api/trade/analyze", json=body)


def _wire(user, credit=None, agent=None):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_credit_service] = lambda: (credit or _FakeCredit())
    app.dependency_overrides[get_trade_analyzer] = lambda: (agent or _fake_agent())


_BODY = {"my_team_id": "me", "give": ["g"], "get": ["x"]}


# ---------------------------------------------------------------------------
# GATE ORDER
# ---------------------------------------------------------------------------
async def test_intro_user_gets_403_and_loses_zero_credits(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="intro", credits=100)
    credit = _FakeCredit()
    _patch_loader(monkeypatch)
    _wire(user, credit)
    try:
        resp = await _post(_BODY)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403
    assert credit.deducts == []           # feature check fired BEFORE any deduct
    assert user.credits_remaining == 100  # zero credits lost


async def test_standard_insufficient_credits_402_no_deduction(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="standard", credits=5)   # cost is 10
    credit = _FakeCredit()
    _patch_loader(monkeypatch)
    _wire(user, credit)
    try:
        resp = await _post(_BODY)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 402
    assert credit.deducts == []
    assert user.credits_remaining == 5


async def test_standard_with_credits_runs_and_deducts_ten_once(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="standard", credits=50)
    credit = _FakeCredit()
    _patch_loader(monkeypatch)
    _wire(user, credit)
    try:
        resp = await _post(_BODY)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert credit.deducts == [("trade_analysis", 10)]
    assert user.credits_remaining == 40
    data = resp.json()
    assert data["winner"] == "you" and data["fairness"] == "lopsided you"
    assert data["rationale"] == "grounded rationale"
    assert data["demo_mode"] is False


# ---------------------------------------------------------------------------
# DEMO BYPASS
# ---------------------------------------------------------------------------
async def test_demo_mode_bypasses_gate_for_intro_user(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="intro", credits=100)   # would 403 in prod
    credit = _FakeCredit()
    _patch_loader(monkeypatch)
    _wire(user, credit)
    try:
        resp = await _post(_BODY)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert credit.deducts == []            # no gate, no deduction
    assert user.credits_remaining == 100
    assert resp.json()["demo_mode"] is True


# ---------------------------------------------------------------------------
# REAL PATH not yet available — 501 AFTER feature, BEFORE deduct
# ---------------------------------------------------------------------------
async def test_real_league_path_501_without_charging(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="pro", credits=200)
    credit = _FakeCredit()
    # loader NOT patched → real load_league_for_analysis raises 501
    _wire(user, credit)
    try:
        resp = await _post(_BODY)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 501
    assert credit.deducts == []
    assert user.credits_remaining == 200


# ---------------------------------------------------------------------------
# VALIDATION before deduction
# ---------------------------------------------------------------------------
async def test_invalid_trade_400_before_deduction(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    user = _make_user(tier="standard", credits=50)
    credit = _FakeCredit()
    _patch_loader(monkeypatch)
    _wire(user, credit)
    try:
        resp = await _post({"my_team_id": "me", "give": [], "get": ["x"]})  # empty side
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert credit.deducts == []          # validation precedes the deduction


# ---------------------------------------------------------------------------
# ROSTER GUARD
# ---------------------------------------------------------------------------
async def test_roster_guard_warns_on_net_overflow(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")   # bypass gate; focus on guard
    user = _make_user(tier="pro", credits=200)
    _patch_loader(monkeypatch, league=_fixture_league(roster_limit=2))
    _wire(user)
    try:
        resp = await _post({"my_team_id": "me", "give": ["g"], "get": ["x", "y"]})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    guard = resp.json()["roster_guard"]
    assert guard["triggered"] is True
    assert guard["net_players"] == 1
    assert [r["name"] for r in guard["drop_recommendations"]] == ["Bench"]


async def test_balanced_swap_no_roster_warning(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    _patch_loader(monkeypatch, league=_fixture_league(roster_limit=2))
    _wire(user)
    try:
        resp = await _post({"my_team_id": "me", "give": ["g"], "get": ["x"]})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["roster_guard"]["triggered"] is False


# ---------------------------------------------------------------------------
# ACCEPTABILITY READ (slice 5) — additive field + "you'd win, they'd reject"
# ---------------------------------------------------------------------------
async def test_response_is_additive_and_carries_acceptability(monkeypatch):
    """The existing payload is unchanged; a new `acceptability` object is added."""
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    _patch_loader(monkeypatch)
    _wire(user)
    try:
        resp = await _post(_BODY)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    # existing fields still present + intact (additive change)
    for f in ("winner", "fairness", "lineup_gain", "value_delta", "give_value", "get_value",
              "confidence", "hedged", "roster_guard", "rationale", "demo_mode"):
        assert f in data
    acc = data["acceptability"]
    assert set(acc) == {"verdict", "their_lineup_gain", "overtake_flag", "hedged", "why"}
    assert acc["verdict"] in {"likely_accept", "marginal", "likely_reject"}


async def test_winning_trade_they_would_reject_is_not_a_win(monkeypatch):
    """The fixture trade (give a 20, get a 90) is a clear win FOR ME — but a
    robbery the opponent would reject. The verdict says winner=you while the
    acceptability read says likely_reject; the read never rounds up to a win."""
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=200)
    _patch_loader(monkeypatch)
    _wire(user)
    try:
        resp = await _post(_BODY)
    finally:
        app.dependency_overrides.clear()
    data = resp.json()
    assert data["winner"] == "you"                       # my lineup jumps (WR 20 → 90)
    assert data["acceptability"]["verdict"] == "likely_reject"
    assert data["acceptability"]["their_lineup_gain"] <= 0   # their lineup falls


# ---------------------------------------------------------------------------
# Guarded end-to-end on the REAL seeded demo league (skips in CI)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not Path("data/cache/weekly_pbp_2025.parquet").exists(),
    reason="real 2025 demo data not on disk (CI)",
)
async def test_demo_route_end_to_end_on_real_league(monkeypatch):
    from backend.database import AsyncSessionLocal
    from backend.services.trade.trade_demo_source import seed_demo_league

    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    try:
        async with AsyncSessionLocal() as db:
            source = await seed_demo_league(db)
    except Exception as exc:
        pytest.skip(f"demo DB unavailable: {exc}")

    state = source.get_league_state()
    me = state.my_team
    opp = next(t for t in state.teams if not t.is_me)
    body = {
        "my_team_id": me.team_id,
        "give": [me.roster[-1].canonical_player_id],   # a weaker piece
        "get": [opp.roster[0].canonical_player_id],    # a stud
    }
    user = _make_user(tier="intro", credits=0)         # demo ignores tier/credits
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_trade_analyzer] = lambda: _fake_agent()
    try:
        resp = await _post(body)
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert data["demo_mode"] is True
    assert data["winner"] in {"you", "opponent", "even"}
    assert data["confidence"] in {"full", "limited", "insufficient"}


# ---------------------------------------------------------------------------
# EMPTY-SLOT WARNING surfaces through the route (additive `warnings` field)
# ---------------------------------------------------------------------------
def _iv_pos(pid, pos, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=pid, position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="usage",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _full_league():
    my = tuple(RosterPlayer(p, p, pos) for p, pos in [
        ("qb", "QB"), ("rb1", "RB"), ("rb2", "RB"), ("rb3", "RB"),
        ("wr1", "WR"), ("wr2", "WR"), ("wr3", "WR"), ("te", "TE")])
    opp = tuple(RosterPlayer(p, p, pos) for p, pos in [("owr", "WR"), ("owr2", "WR")])
    state = LeagueState(2025, 14, (TeamState("me", "Me", True, my),
                                   TeamState("opp", "Opp", False, opp)))
    specs = [("qb", "QB", 30), ("rb1", "RB", 40), ("rb2", "RB", 35), ("rb3", "RB", 20),
             ("wr1", "WR", 38), ("wr2", "WR", 34), ("wr3", "WR", 30), ("te", "TE", 25),
             ("owr", "WR", 45), ("owr2", "WR", 44)]
    return state, {p: _iv_pos(p, pos, fv) for p, pos, fv in specs}, 16


async def test_empty_slot_warning_surfaces_in_response(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=100)
    state, values, rl = _full_league()
    _patch_loader(monkeypatch, (state, values, rl))
    _wire(user)
    try:
        # ship the only TE for two WRs → empties the TE slot
        resp = await _post({"my_team_id": "me", "give": ["te"], "get": ["owr", "owr2"]})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["warnings"], list)                       # additive list field
    assert len(data["warnings"]) == 1
    w = data["warnings"][0]
    assert w["type"] == "empty_required_slot" and w["position"] == "TE"
    assert "only TE" in w["message"]


async def test_filled_trade_has_empty_warnings_list(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    user = _make_user(tier="pro", credits=100)
    state, values, rl = _full_league()
    _patch_loader(monkeypatch, (state, values, rl))
    _wire(user)
    try:
        resp = await _post({"my_team_id": "me", "give": ["wr3"], "get": ["owr"]})  # keeps all slots
    finally:
        app.dependency_overrides.clear()
    data = resp.json()
    assert data["warnings"] == []                                   # present, empty — additive
