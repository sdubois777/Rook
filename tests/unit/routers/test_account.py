"""Tests for backend/routers/account.py"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.models.user import User


def _make_user(
    tier="standard",
    credits=50,
    external_id="dev-user-001",
    email="dev-user-001@dev.local",
):
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.external_id = external_id
    user.email = email
    user.display_name = "Test User"
    user.tier = tier
    user.credits_remaining = credits
    user.subscription_status = None
    user.deleted_at = None
    user.created_at = datetime.now(timezone.utc)
    user.updated_at = datetime.now(timezone.utc)
    return user


def _make_league(user_id=None):
    league = MagicMock()
    league.id = uuid.uuid4()
    league.user_id = user_id or uuid.uuid4()
    league.platform = "yahoo"
    league.league_id = "test-league-123"
    league.league_name = "Test League"
    league.team_count = 12
    league.draft_type = "auction"
    league.scoring = "ppr"
    league.budget = 200
    league.season_year = 2026
    league.is_active = True
    league.suspended_at = None
    league.last_synced = None
    league.created_at = datetime.now(timezone.utc)
    return league


@pytest.mark.asyncio
async def test_get_me_returns_user():
    user = _make_user(tier="standard", credits=50)

    from backend.core.dependencies import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/account/me")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "standard"
    assert data["credits_remaining"] == 50
    assert data["email"] == user.email
    assert "tier_limits" in data


@pytest.mark.asyncio
async def test_get_credits_returns_balance():
    user = _make_user(tier="standard", credits=42)

    from backend.core.dependencies import get_current_user, get_credit_service

    mock_service = AsyncMock()
    mock_service.get_usage_history.return_value = []

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_credit_service] = lambda: mock_service

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/account/credits")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["balance"] == 42
    assert data["monthly_allowance"] == 20  # standard tier


@pytest.mark.asyncio
async def test_add_league_succeeds_within_limit():
    user = _make_user(tier="standard", credits=50)
    league = _make_league(user_id=user.id)

    from backend.core.dependencies import get_current_user, get_league_service

    mock_service = AsyncMock()
    mock_service.count_active_leagues.return_value = 0  # 0 active, limit 2
    mock_service.add_league.return_value = league

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_league_service] = lambda: mock_service

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/account/leagues",
                json={
                    "platform": "yahoo",
                    "league_id": "test-league-123",
                    "team_count": 12,
                    "draft_type": "auction",
                    "scoring": "ppr",
                    "budget": 200,
                    "season_year": 2026,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 201
    data = resp.json()
    assert data["platform"] == "yahoo"
    assert data["team_count"] == 12


@pytest.mark.asyncio
async def test_add_league_respects_tier_limit():
    user = _make_user(tier="intro", credits=0)

    from backend.core.dependencies import get_current_user, get_league_service

    mock_service = AsyncMock()
    mock_service.count_active_leagues.return_value = 1  # 1 active, intro limit is 1

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_league_service] = lambda: mock_service

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/account/leagues",
                json={
                    "platform": "yahoo",
                    "league_id": "new-league",
                    "team_count": 12,
                    "draft_type": "auction",
                    "scoring": "ppr",
                    "budget": 200,
                    "season_year": 2026,
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403
    data = resp.json()
    assert data["error"] == "league_limit_reached"


@pytest.mark.asyncio
async def test_delete_league_hard_deletes():
    user = _make_user(tier="standard")
    league = _make_league(user_id=user.id)

    from backend.core.dependencies import get_current_user, get_league_service

    mock_service = AsyncMock()
    mock_service.delete_league.return_value = None

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_league_service] = lambda: mock_service

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.delete(f"/api/account/leagues/{league.id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 204
    mock_service.delete_league.assert_awaited_once_with(user.id, league.id)


@pytest.mark.asyncio
async def test_user_cannot_delete_other_users_league():
    """When league doesn't belong to user, service raises NotFoundError."""
    user = _make_user(tier="standard")
    other_league_id = uuid.uuid4()

    from backend.core.dependencies import get_current_user, get_league_service
    from backend.core.exceptions import NotFoundError

    mock_service = AsyncMock()
    mock_service.delete_league.side_effect = NotFoundError(
        f"League {other_league_id} not found"
    )

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_league_service] = lambda: mock_service

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.delete(f"/api/account/leagues/{other_league_id}")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ---------------------------------------------------------------------------
# Over-limit chooser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_limit_state_reports_over_limit():
    user = _make_user(tier="standard")
    leagues = [_make_league(user.id) for _ in range(3)]

    from backend.core.dependencies import get_current_user, get_db

    fake_recon = AsyncMock()
    fake_recon.limit_state.return_value = {
        "over_limit": True, "active_count": 3, "max_leagues": 2,
        "candidates": leagues,
    }

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with patch("backend.routers.account._reconciler",
               return_value=(fake_recon, AsyncMock())):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/account/leagues/limit-state")
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["over_limit"] is True
    assert data["active_count"] == 3
    assert data["max_leagues"] == 2
    assert len(data["candidates"]) == 3


@pytest.mark.asyncio
async def test_resolve_limit_keeps_parks_and_commits():
    user = _make_user(tier="standard")
    leagues = [_make_league(user.id) for _ in range(3)]

    from backend.core.dependencies import get_current_user, get_db

    fake_recon = AsyncMock()
    fake_recon.limit_state.return_value = {
        "over_limit": False, "active_count": 2, "max_leagues": 2,
        "candidates": leagues[:2],
    }
    fake_repo = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with patch("backend.routers.account._reconciler",
               return_value=(fake_recon, fake_repo)):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/account/leagues/resolve-limit",
                    json={"keep": [str(leagues[0].id), str(leagues[1].id)]},
                )
        finally:
            app.dependency_overrides.clear()

    assert resp.status_code == 200
    fake_recon.resolve_keep.assert_awaited_once()
    fake_repo.commit.assert_awaited_once()
    assert resp.json()["over_limit"] is False
