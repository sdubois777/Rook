"""
Waiver router — GET /api/waiver/league (demo gate) + POST /api/waiver/recommendations.

Proves the demo gate (404 with WAIVER_DEMO_MODE off) and, with the source + news
mocked, that /recommendations returns the shaped payload. The engine math itself is
covered purely in tests/unit/services/waiver; here we exercise the wiring only.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pandas as pd
from httpx import ASGITransport, AsyncClient

import backend.routers.waiver as waiver_mod
from backend.core.dependencies import get_credit_service, get_current_user, get_db
from backend.main import app
from backend.models.user import User
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend
from backend.services.waiver.waiver_demo_source import WaiverDemoSource


def _user():
    u = MagicMock(spec=User)
    u.id = uuid.uuid4()
    u.tier = "pro"
    u.credits_remaining = 100
    return u


def _iv(pid, fv, ppg, *, pos="WR"):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=8, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=ppg, expected_ppg=ppg, opportunity_gap=0.0, sustainable=True,
        forward_ppg=ppg, schedule_modifier=0.0, prior_projection=None, prior_weight=0.0,
        name_bias_guard_applied=False, confidence=Confidence.FULL, confidence_reason="",
    )


def _source():
    me = TeamState("me", "You", True, (RosterPlayer("a", "A", "WR", nfl_team="SF", starter_slot="WR1"),))
    state = LeagueState(2025, 14, (me,))
    pool = [RosterPlayer("b", "B", "WR", nfl_team="CIN")]
    values = {"a": _iv("a", 40, 8), "b": _iv("b", 92, 18)}
    return WaiverDemoSource(
        state=state, pool=pool, values=values, weekly_usage=pd.DataFrame(), priors={},
        faab_remaining_by_team={"me": 50},
    )


async def test_league_404_when_demo_off(monkeypatch):
    monkeypatch.delenv("WAIVER_DEMO_MODE", raising=False)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/waiver/league")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


async def test_recommendations_shape_with_mocked_source(monkeypatch):
    monkeypatch.setenv("WAIVER_DEMO_MODE", "true")

    async def _fake_source(db, demo, user=None):
        return _source()

    async def _fake_news(db, pool_ids, **kw):
        return {}

    monkeypatch.setattr(waiver_mod, "load_waiver_source", _fake_source)
    monkeypatch.setattr(waiver_mod, "build_news_map", _fake_news)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_credit_service] = lambda: MagicMock()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/api/waiver/recommendations", json={"my_team_id": "me"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["season"] == 2025 and data["week"] == 14
    assert data["my_team_id"] == "me" and data["demo_mode"] is True
    assert data["waiver"]["type"] == "faab" and data["waiver"]["remaining"] == 50
    # The stronger pool WR should surface as a recommendation.
    assert isinstance(data["recommendations"], list) and len(data["recommendations"]) >= 1
    top = data["recommendations"][0]
    assert top["add"]["id"] == "b" and top["lineup_delta_ppw"] > 0
    assert top["faab"]["total_bid"] <= 50
