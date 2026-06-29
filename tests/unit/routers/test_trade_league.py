"""
Tests for GET /api/trade/league (read-only picker support, demo-only).

Proves: 404 when TRADE_DEMO_MODE is off (no real-league exposure here), and that
when on it reshapes the SAME seeded LeagueState + evaluate_league output — picker
values equal verdict values.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from httpx import ASGITransport, AsyncClient

import backend.routers.trade as trade_mod
from backend.core.dependencies import get_current_user, get_db
from backend.main import app
from backend.models.user import User
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _user():
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.tier = "intro"
    u.credits_remaining = 0
    return u


def _iv(pid, fv, *, conf=Confidence.FULL, trend=ValueTrend.STABLE, buy=False, sell=False):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position="WR", forward_value=fv,
        value_trend=trend, buy_low=buy, sell_high=sell, why="usage", games_played=10,
        usage_recent=0.5, usage_prior=0.5, usage_delta=0.0, recency_ppg=fv / 5,
        expected_ppg=fv / 5, opportunity_gap=0.0, sustainable=True, forward_ppg=fv / 5,
        schedule_modifier=0.0, prior_projection=None, prior_weight=0.0,
        name_bias_guard_applied=False, confidence=conf, confidence_reason="",
    )


def _fixture():
    me = TeamState("me", "You", True, (RosterPlayer("g", "Give", "WR", nfl_team="SF"),))
    opp = TeamState("opp", "Rivals", False, (RosterPlayer("x", "Get", "WR", nfl_team="CIN"),))
    state = LeagueState(2025, 14, (me, opp))
    values = {
        "g": _iv("g", 20, trend=ValueTrend.FALLING, sell=True),
        "x": _iv("x", 90, conf=Confidence.LIMITED, buy=True),
    }
    return state, values, 16


async def _get():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get("/api/trade/league")


async def test_league_404_when_demo_off(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        resp = await _get()
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


async def test_league_returns_teams_rosters_and_values(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")

    async def _fake(db, user, demo):
        return _fixture()
    monkeypatch.setattr(trade_mod, "load_league_for_analysis", _fake)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        resp = await _get()
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["demo_mode"] is True and data["season"] == 2025 and data["week"] == 14
    me = next(t for t in data["teams"] if t["is_me"])
    assert me["team_name"] == "You"
    g = me["roster"][0]
    # picker value bundle mirrors the engine output exactly
    assert g["id"] == "g" and g["forward_value"] == 20 and g["value_trend"] == "falling"
    assert g["sell_high"] is True and g["nfl_team"] == "SF"
    opp = next(t for t in data["teams"] if not t["is_me"])
    x = opp["roster"][0]
    assert x["confidence"] == "limited" and x["buy_low"] is True
