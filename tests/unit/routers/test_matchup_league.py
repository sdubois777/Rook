"""
Tests for GET /api/matchup/league (read-only H2H scouting, demo-only).

Proves: 404 when TRADE_DEMO_MODE off; when on, returns the synthesized pairing +
strength ladder + opponent scout on the SAME evaluate_league basis; the grid sums
to each team's ppw; and — the funnel invariant — the whole path is NON-METERED
(no credit_service dependency, no Sonnet/agent import anywhere it reaches).
"""
from __future__ import annotations

import inspect
import uuid
from pathlib import Path
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
    u.tier = "pro"
    u.credits_remaining = 999
    return u


def _iv(pid, pos, fv, ppg, *, conf=Confidence.FULL):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="", games_played=10,
        usage_recent=0.5, usage_prior=0.5, usage_delta=0.0, recency_ppg=ppg, expected_ppg=ppg,
        opportunity_gap=0.0, sustainable=True, forward_ppg=ppg, schedule_modifier=0.0,
        prior_projection=None, prior_weight=0.0, name_bias_guard_applied=False,
        confidence=conf, confidence_reason="",
    )


def _team(tid, name, is_me, players):
    roster = tuple(RosterPlayer(p[0], p[0].upper(), p[1], nfl_team="SF") for p in players)
    return TeamState(tid, name, is_me, roster)


def _fixture():
    # 4 teams, each with a QB/2RB/3WR/TE core so optimal_lineup fills real slots.
    def core(prefix, strong):
        base = 16.0 if strong else 9.0
        return [
            (f"{prefix}_qb", "QB"), (f"{prefix}_rb1", "RB"), (f"{prefix}_rb2", "RB"),
            (f"{prefix}_wr1", "WR"), (f"{prefix}_wr2", "WR"), (f"{prefix}_wr3", "WR"),
            (f"{prefix}_te", "TE"),
        ], base
    teams, values = [], {}
    specs = [("me", "You", True, True), ("a", "Rivals A", False, False),
             ("b", "Rivals B", False, True), ("c", "Rivals C", False, False)]
    for tid, name, is_me, strong in specs:
        players, base = core(tid, strong)
        teams.append(_team(tid, name, is_me, players))
        for i, (pid, pos) in enumerate(players):
            values[pid] = _iv(pid, pos, fv=80 - i * 4, ppg=base - i)
    state = LeagueState(2025, 14, tuple(teams))
    return state, values, 16


async def _get(params=""):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(f"/api/matchup/league{params}")


async def test_404_when_demo_off(monkeypatch):
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        resp = await _get()
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


async def test_returns_pairing_ladder_and_scout(monkeypatch):
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
    d = resp.json()
    assert d["demo_mode"] is True and d["week"] == 14 and d["my_team_name"] == "You"
    # ladder: all 4 teams, sorted by strength desc.
    assert len(d["teams"]) == 4
    strengths = [t["strength"] for t in d["teams"]]
    assert strengths == sorted(strengths, reverse=True)
    # pairing: every team exactly once (2 pairs).
    assert len(d["matchups"]) == 2
    paired = sorted(t for m in d["matchups"] for t in (m["home_team_id"], m["away_team_id"]))
    assert paired == ["a", "b", "c", "me"]
    # scout present, grid sums to each side's ppw (the cross-page invariant).
    sc = d["scout"]
    assert sc["opponent_team_id"] in {"a", "b", "c"}
    assert sc["band_is_approximate"] is True
    assert round(sum(g["mine"] for g in sc["grid"]), 2) == sc["my_ppw"]
    assert round(sum(g["theirs"] for g in sc["grid"]), 2) == sc["opp_ppw"]
    assert round(sc["my_ppw"] - sc["opp_ppw"], 2) == sc["margin"]
    # Leverage: value-gated + reciprocal. A position never appears in both a team's
    # needs and its (value-gated) surplus, and the mirror flag is a real bool.
    assert isinstance(sc["is_reciprocal_fit"], bool)
    assert not (set(sc["my_needs"]) & set(sc["my_surplus_positions"]))
    assert not (set(sc["opp_needs"]) & set(sc["opp_surplus_positions"]))


async def test_acting_as_switch_scouts_that_team(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")

    async def _fake(db, user, demo):
        return _fixture()
    monkeypatch.setattr(trade_mod, "load_league_for_analysis", _fake)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        resp = await _get("?my_team_id=b")
    finally:
        app.dependency_overrides.clear()
    d = resp.json()
    assert d["my_team_id"] == "b"
    assert d["scout"]["opponent_team_id"] != "b"   # b's opponent, not itself


def test_matchup_path_is_non_metered():
    """FUNNEL INVARIANT: the matchup endpoint takes NO credit_service dependency,
    and neither the router nor the scouting service imports Sonnet/agent/anything
    token-metered. A page load can never cross the paywall."""
    from backend.routers import matchup as m

    # 1. The route signature has no credit_service / agent dependency.
    params = set(inspect.signature(m.league).parameters)
    assert "credit_service" not in params
    assert not any("agent" in p for p in params)

    # 2. No metered symbol anywhere the page-load path reaches.
    banned = ("anthropic", "BaseAgent", "run_agent", "credit_service", "explain_trade",
              "generate_candidates", "get_credit_service")
    for path in (
        Path("backend/routers/matchup.py"),
        Path("backend/services/matchup/scouting.py"),
        Path("backend/services/matchup/__init__.py"),
    ):
        src = path.read_text(encoding="utf-8")
        hits = [b for b in banned if b in src]
        assert not hits, f"{path} references metered symbol(s): {hits}"
