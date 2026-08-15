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
    user.tier_expires_at = None
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
    league.draft_date = None
    league.manager_map = {"1": "Team One", "2": "Team Two"}
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
    assert data["monthly_allowance"] == 0  # monthly grants are deleted


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
    user = _make_user(tier="free", credits=0)

    from backend.core.dependencies import get_current_user, get_league_service

    mock_service = AsyncMock()
    mock_service.count_active_leagues.return_value = 1  # 1 active, free limit is 1

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


# ---------------------------------------------------------------------------
# Referral state
# ---------------------------------------------------------------------------

def _referrer_state(count=2, percent_off=7):
    """The dict ReferralService.referrer_state returns.

    percent_off is deliberately NOT a rate the real program can produce: the
    endpoint must pass the service's number through, not compute or correct one.
    The cap and the per-referral rate come from REFERRAL_PROGRAM, as they do in
    the real service."""
    from backend.models.user import REFERRAL_PROGRAM

    return {
        "code": "ROOK-7K2M9X",
        "referral_count": count,
        "percent_off": percent_off,
        "percent_off_cap": REFERRAL_PROGRAM["referrer_percent_off_cap"],
        "percent_off_per_referral": REFERRAL_PROGRAM[
            "referrer_percent_off_per_referral"
        ],
    }


async def _get_referral(user, state, db=None):
    """Call GET /account/referral with ReferralService.from_session stubbed."""
    from backend.core.dependencies import get_current_user, get_db

    service = AsyncMock()
    service.referrer_state.return_value = state

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: (db if db is not None else AsyncMock())
    with patch(
        "backend.services.referral_service.ReferralService.from_session",
        return_value=service,
    ):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/account/referral")
        finally:
            app.dependency_overrides.clear()
    return resp, service


@pytest.mark.asyncio
async def test_referral_returns_the_callers_own_state():
    user = _make_user(tier="standard")
    user.subscription_status = "active"
    state = _referrer_state(count=2, percent_off=7)

    resp, service = await _get_referral(user, state)

    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == "ROOK-7K2M9X"
    assert data["referral_count"] == 2
    assert data["percent_off"] == 7
    assert data["percent_off_cap"] == state["percent_off_cap"]
    assert data["percent_off_per_referral"] == state["percent_off_per_referral"]
    # Scoped by the authenticated user id and nothing else.
    service.referrer_state.assert_awaited_once_with(user.id)


@pytest.mark.asyncio
async def test_referral_share_url_carries_the_code_as_a_ref_param():
    from backend.config import settings

    user = _make_user(tier="standard")
    resp, _ = await _get_referral(user, _referrer_state())

    assert resp.json()["share_url"] == f"{settings.app_url}/?ref=ROOK-7K2M9X"


@pytest.mark.asyncio
async def test_referral_never_reveals_who_redeemed():
    """A count and a rate, never a list. The people who redeemed the code did
    not consent to having their purchase disclosed to the referrer."""
    user = _make_user(tier="standard")
    resp, _ = await _get_referral(user, _referrer_state(count=3, percent_off=9))

    assert set(resp.json()) == {
        "code", "share_url", "referral_count", "percent_off",
        "percent_off_cap", "percent_off_per_referral", "eligible",
    }


@pytest.mark.asyncio
async def test_referral_commits_so_a_new_code_survives_the_request():
    """Codes are minted lazily on first read and the service does not commit."""
    user = _make_user(tier="standard")
    db = AsyncMock()

    resp, _ = await _get_referral(user, _referrer_state(), db=db)

    assert resp.status_code == 200
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_referral_is_eligible_on_a_monthly_subscription():
    user = _make_user(tier="standard")
    user.subscription_status = "active"
    user.tier_expires_at = None

    resp, _ = await _get_referral(user, _referrer_state())

    assert resp.json()["eligible"] is True


@pytest.mark.asyncio
async def test_referral_is_not_eligible_on_the_free_tier():
    """Nothing to attach a recurring coupon to. The rate is still reported —
    it is earned and it applies once they take a monthly plan."""
    user = _make_user(tier="free")
    user.subscription_status = None

    resp, _ = await _get_referral(user, _referrer_state(count=1, percent_off=6))

    data = resp.json()
    assert data["eligible"] is False
    assert data["percent_off"] == 6


@pytest.mark.asyncio
async def test_referral_is_not_eligible_on_a_season_pass():
    """A season pass is a one-time payment with no recurring invoice, and the
    program covers monthly intervals only."""
    from datetime import timedelta

    user = _make_user(tier="pro")
    user.subscription_status = "active"
    user.tier_expires_at = datetime.now(timezone.utc) + timedelta(days=90)

    resp, _ = await _get_referral(user, _referrer_state())

    assert resp.json()["eligible"] is False


@pytest.mark.asyncio
async def test_get_connected_platforms():
    """GET /account/credentials returns the platforms with stored credentials."""
    from unittest.mock import AsyncMock, patch
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    with patch("backend.repositories.credential_repo.CredentialRepository") as MockRepo:
        repo = AsyncMock()
        repo.list_platforms = AsyncMock(return_value=["espn", "yahoo"])
        MockRepo.return_value = repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.get("/api/account/credentials")
        finally:
            app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"platforms": ["espn", "yahoo"]}
