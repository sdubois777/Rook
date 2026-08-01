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
    u.tier_expires_at = None
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


async def test_league_404_when_no_synced_league(monkeypatch):
    """Un-gated: demo off + no synced league → 404 (real seam returns None)."""
    monkeypatch.delenv("WAIVER_DEMO_MODE", raising=False)

    async def _none(db, u):
        return None
    monkeypatch.setattr(
        "backend.services.trade.real_league_source.build_real_waiver_source", _none
    )
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/waiver/league")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404


async def test_league_409_undrafted(monkeypatch):
    """Un-gated: demo off + undrafted league → 409 error=undrafted_league."""
    from backend.core.exceptions import UndraftedLeagueError
    monkeypatch.delenv("WAIVER_DEMO_MODE", raising=False)

    async def _undrafted(db, u):
        raise UndraftedLeagueError("inferred")
    monkeypatch.setattr(
        "backend.services.trade.real_league_source.build_real_waiver_source", _undrafted
    )
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/waiver/league")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 409
    assert resp.json()["error"] == "undrafted_league"


def _wire_source():
    """A source whose pool includes a NULL-team player and out-of-order ppg, to prove
    the wire endpoint sorts by forward_ppg desc and never drops the null-team FA."""
    me = TeamState("me", "You", True, (RosterPlayer("a", "A", "WR", nfl_team="SF"),))
    state = LeagueState(2025, 3, (me,))
    pool = [
        RosterPlayer("b", "Mid WR", "WR", nfl_team="CIN"),
        RosterPlayer("c", "Top QB", "QB", nfl_team="LV"),
        RosterPlayer("d", "No Team WR", "WR", nfl_team=None),   # nfl_team null on purpose
    ]
    values = {
        "a": _iv("a", 40, 8, pos="WR"),
        "b": _iv("b", 60, 11, pos="WR"),
        "c": _iv("c", 24, 17, pos="QB"),
        "d": _iv("d", 30, 9, pos="WR"),
    }
    return WaiverDemoSource(
        state=state, pool=pool, values=values, weekly_usage=pd.DataFrame(), priors={},
        faab_remaining_by_team={"me": 50},
    )


async def test_wire_free_sorted_and_keeps_null_team(monkeypatch):
    """The FREE browse list: sorted by forward_ppg desc, null-team player PRESENT with
    null (not dropped), and NO credit service is even wired (browsing never debits)."""
    monkeypatch.setenv("WAIVER_DEMO_MODE", "true")

    async def _fake_source(db, demo, user=None):
        return _wire_source()

    monkeypatch.setattr(waiver_mod, "load_waiver_source", _fake_source)
    # Intro tier has NO waiver_wire feature — the free wire must still 200.
    intro = _user(); intro.tier = "intro"; intro.credits_remaining = 0
    app.dependency_overrides[get_current_user] = lambda: intro
    app.dependency_overrides[get_db] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/waiver/wire")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200  # free — no 403 for a feature-less tier, no 402 at 0 credits
    data = resp.json()
    assert data["season"] == 2025 and data["week"] == 3 and data["demo_mode"] is True
    players = data["players"]
    assert len(players) == 3  # only the 3 pool players (roster excluded)
    # Sorted by forward_ppg desc: QB(17) > No-Team WR? no — Mid WR(11) > No-Team WR(9).
    assert [p["id"] for p in players] == ["c", "b", "d"]
    assert [p["forward_ppg"] for p in players] == [17.0, 11.0, 9.0]
    # Null-team FA is present, emitted as null — never dropped.
    null_row = next(p for p in players if p["id"] == "d")
    assert null_row["nfl_team"] is None
    assert {p["nfl_team"] for p in players} == {"LV", "CIN", None}


async def test_wire_never_debits_credits(monkeypatch):
    """Even a credit-service override that would explode if called proves browsing is
    un-metered — the endpoint doesn't depend on get_credit_service at all."""
    monkeypatch.setenv("WAIVER_DEMO_MODE", "true")

    async def _fake_source(db, demo, user=None):
        return _wire_source()

    exploding = MagicMock()
    exploding.deduct.side_effect = AssertionError("wire must not debit credits")
    monkeypatch.setattr(waiver_mod, "load_waiver_source", _fake_source)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_credit_service] = lambda: exploding
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/waiver/wire")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    exploding.deduct.assert_not_called()


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


# ---------------------------------------------------------------------------
# Real waiver settings — what the response is allowed to say about money.
#
# The recommendation charges 2 credits BEFORE any work is done and there is no
# refund path anywhere, so a league shape that makes this endpoint raise costs the
# customer real money for nothing. The non-bidding case below is exactly that
# shape: it returned a 500 after the charge until faab.suggest_bid learned to
# accept "this league does not bid".
# ---------------------------------------------------------------------------
def _real_source(**waiver):
    """A RealWaiverSource (NOT the demo source) so the waiver settings under test
    are the ones the real per-league path produces."""
    from backend.services.trade.real_league_source import RealWaiverSource

    me = TeamState("t1", "You", True,
                   (RosterPlayer("a", "A", "WR", nfl_team="SF", starter_slot="WR1"),))
    state = LeagueState(2025, 14, (me,))
    pool = [RosterPlayer("b", "B", "WR", nfl_team="CIN")]
    values = {"a": _iv("a", 40, 8), "b": _iv("b", 92, 18)}
    return RealWaiverSource(
        state=state, pool=pool, values=values, weekly_usage=pd.DataFrame(),
        priors={}, roster_limit=15, **waiver,
    )


async def _post_recommendations(monkeypatch, src):
    """Drive POST /recommendations on the REAL path (demo off), so the credit charge
    is actually enforced and a raise after it would be a real charge for nothing."""
    from unittest.mock import AsyncMock

    monkeypatch.delenv("WAIVER_DEMO_MODE", raising=False)

    async def _fake_source(db, demo, user=None, **kw):
        return src

    async def _fake_news(db, pool_ids, **kw):
        return {}

    monkeypatch.setattr(waiver_mod, "load_waiver_source", _fake_source)
    monkeypatch.setattr(waiver_mod, "build_news_map", _fake_news)
    credits = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_credit_service] = lambda: credits
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/api/waiver/recommendations", json={"my_team_id": "t1"})
    finally:
        app.dependency_overrides.clear()
    return resp, credits


async def test_non_bidding_league_gets_no_dollar_figure_and_does_not_500(monkeypatch):
    """A rolling-priority league. The customer HAS been charged by this point, so a
    200 with a usable ranking is the only acceptable outcome — and not one dollar
    amount may appear."""
    src = _real_source(
        waiver_type="rolling priority", uses_bidding_budget=False,
        faab_budget=None, waiver_position_by_team={"t1": 3},
    )
    resp, credits = await _post_recommendations(monkeypatch, src)

    assert credits.charge_metered.await_count == 1     # the customer paid
    assert resp.status_code == 200                      # ...and got something back
    w = resp.json()["waiver"]
    assert w["uses_bidding_budget"] is False
    assert w["budget"] is None and w["remaining"] is None
    assert w["budget_is_assumed"] is False
    assert w["waiver_position"] == 3
    assert w["type"] == "rolling priority"

    # The ranking and the tier survive; the money does not.
    recs = resp.json()["recommendations"]
    assert len(recs) >= 1
    faab = recs[0]["faab"]
    assert faab["bid_applicable"] is False
    assert faab["total_bid"] == 0 and faab["news_bump_bid"] == 0
    assert faab["tier_label"] not in ("", None)
    assert "$" not in faab["why"]


async def test_bidding_league_with_unreadable_budget_labels_the_assumption(monkeypatch):
    """The league bids but no budget could be read. A figure is still needed for the
    bid curve, so the standard $100 is used — and the response SAYS it is assumed.
    Presenting it as the league's own number is the defect this replaces."""
    src = _real_source(waiver_type="faab", uses_bidding_budget=True, faab_budget=None)
    resp, _ = await _post_recommendations(monkeypatch, src)

    assert resp.status_code == 200
    w = resp.json()["waiver"]
    assert w["budget_is_assumed"] is True
    assert w["remaining"] == 100
    assert w["budget"] is None          # the league's own budget is still unknown


async def test_substituting_the_full_budget_for_an_unknown_balance_is_an_assumption(monkeypatch):
    """The league bids and its budget WAS read, but this team's spend was never
    reported — so the team's balance is unknown. Using the whole budget in its place
    assumes the team has not spent a cent, and that must be declared.

    Reported unflagged, this is the original defect with a different number: the page
    prints "$200 of $200 budget left" as the customer's real balance. GET
    /waiver/league already returns null for the same team, so leaving this unflagged
    also makes the two endpoints contradict each other in the same session."""
    src = _real_source(
        waiver_type="budget", uses_bidding_budget=True, faab_budget=200,
        faab_remaining_by_team={},       # platform reported nobody's spend
    )
    resp, _ = await _post_recommendations(monkeypatch, src)

    assert resp.status_code == 200
    w = resp.json()["waiver"]
    assert w["budget_is_assumed"] is True
    assert w["budget_basis"] == "full_budget"
    assert w["remaining"] == 200          # still needed to size the bid curve
    assert w["budget"] == 200


async def test_unknown_waiver_system_says_the_SYSTEM_is_the_unknown(monkeypatch):
    """Every league row has these settings NULL until its next sync, because the
    migration does no backfill — so this is the state the whole installed base is in
    on release day, not an edge case.

    A dollar figure is still produced to rank with, but the client must be told the
    unknown is whether the league bids AT ALL, not merely the amount. Saying only
    "we could not read your budget" asserts the league bids, which is the one claim
    that requires the waiver-system field."""
    src = _real_source(waiver_type=None, uses_bidding_budget=None, faab_budget=None)
    resp, _ = await _post_recommendations(monkeypatch, src)

    assert resp.status_code == 200
    w = resp.json()["waiver"]
    assert w["uses_bidding_budget"] is None
    assert w["budget_is_assumed"] is True
    assert w["budget_basis"] == "unknown_system"
    assert w["budget"] is None


async def test_non_bidding_league_reports_no_bidding_as_its_basis(monkeypatch):
    src = _real_source(waiver_type="rolling priority", uses_bidding_budget=False)
    resp, _ = await _post_recommendations(monkeypatch, src)

    w = resp.json()["waiver"]
    assert w["budget_basis"] == "no_bidding"
    assert w["budget_is_assumed"] is False   # nothing was assumed; nothing was claimed


async def test_known_budget_is_never_labelled_an_assumption(monkeypatch):
    """The league's real budget and the team's real balance. Nothing is assumed, so
    the client must not be told anything is."""
    src = _real_source(
        waiver_type="budget", uses_bidding_budget=True, faab_budget=200,
        faab_remaining_by_team={"t1": 137},
    )
    resp, _ = await _post_recommendations(monkeypatch, src)

    assert resp.status_code == 200
    w = resp.json()["waiver"]
    assert w["budget"] == 200 and w["remaining"] == 137
    assert w["budget_is_assumed"] is False
    assert w["budget_basis"] == "league"
    assert resp.json()["recommendations"][0]["faab"]["bid_applicable"] is True


async def test_league_endpoint_leaves_an_unknown_balance_null(monkeypatch):
    """GET /waiver/league. A team whose spend the platform did not report has an
    UNKNOWN balance. It must come back null — filling it with the league budget is
    literally what printed "$100 of $100 budget left" for everyone."""
    src = _real_source(
        waiver_type="budget", uses_bidding_budget=True, faab_budget=200,
        faab_remaining_by_team={},      # nobody's spend was reported
    )
    monkeypatch.delenv("WAIVER_DEMO_MODE", raising=False)

    async def _fake_source(db, demo, user=None, **kw):
        return src

    monkeypatch.setattr(waiver_mod, "load_waiver_source", _fake_source)
    app.dependency_overrides[get_current_user] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/waiver/league")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["faab_budget"] == 200
    assert body["uses_bidding_budget"] is True
    assert body["teams"][0]["faab_remaining"] is None      # NOT 200


async def test_demo_source_still_reports_itself_as_a_bidding_league(monkeypatch):
    """The demo league is a made-up FAAB league with internally consistent numbers.
    It must keep showing them rather than reading as "unknown" and hiding the budget
    it exists to demonstrate."""
    monkeypatch.setenv("WAIVER_DEMO_MODE", "true")

    async def _fake_source(db, demo, user=None, **kw):
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

    w = resp.json()["waiver"]
    assert w["uses_bidding_budget"] is True
    assert w["remaining"] == 50 and w["budget_is_assumed"] is False
    assert resp.json()["recommendations"][0]["faab"]["bid_applicable"] is True
