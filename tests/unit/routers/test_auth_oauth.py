"""Tests for Yahoo OAuth multi-user flow.

Callback identity binding (F1/F2/F6): state is HMAC-signed AND bound to the
initiating browser via a single-use nonce cookie. The callback rejects any state
whose signature, freshness, or browser-binding does not check out — including the
core attack, a validly-signed state for user A replayed by a browser that carries
no matching cookie.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.config import settings
from backend.main import app
from backend.models.user import User
from backend.routers.auth import _NONCE_COOKIE, _sign_state


def _make_user(uid=None):
    user = MagicMock(spec=User)
    user.id = uid or uuid.uuid4()
    user.external_id = "clerk-test"
    user.email = "test@test.com"
    user.tier = "standard"
    user.tier_expires_at = None
    return user


# Default browser-binding nonce used across the callback tests. The cookie the
# browser presents must hash to the value signed into the state.
_NONCE = "browser-binding-nonce-abc123"


def _cookie(nonce: str = _NONCE) -> dict:
    """Cookie header carrying the binding nonce (set explicitly so the httpx jar's
    Secure-over-http filtering never hides it)."""
    return {"Cookie": f"{_NONCE_COOKIE}={nonce}"}


def _mock_db_with_user(user):
    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = user
    mock_db.execute = AsyncMock(return_value=mock_result)
    return mock_db


# ---------------------------------------------------------------------------
# Initiation — connect-url sets the binding cookie and a signed state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_url_sets_binding_cookie_and_signed_state():
    """connect-url returns a signed state and sets an HttpOnly/Secure/Lax nonce
    cookie scoped to /api/auth/yahoo; the state verifies against that cookie."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db
    app.dependency_overrides[get_current_user] = lambda: user

    with patch.object(settings, "yahoo_client_id", "test-client-id"):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as ac:
                resp = await ac.get("/api/auth/yahoo/connect-url")
            assert resp.status_code == 200
            url = resp.json()["url"]
            assert "state=" in url

            set_cookie = resp.headers.get("set-cookie", "")
            assert f"{_NONCE_COOKIE}=" in set_cookie
            assert "HttpOnly" in set_cookie
            assert "Secure" in set_cookie
            assert "samesite=lax" in set_cookie.lower()
            assert "Path=/api/auth/yahoo" in set_cookie

            # The issued state must verify against the issued cookie (end-to-end).
            state = url.split("state=")[1].split("&")[0]
            nonce = set_cookie.split(f"{_NONCE_COOKIE}=")[1].split(";")[0]

            app.dependency_overrides[get_db] = lambda: _mock_db_with_user(user)
            with patch(
                "backend.routers.auth.exchange_code_for_tokens",
                new_callable=AsyncMock,
                return_value={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
            ), patch("backend.routers.auth.CredentialRepository") as MockRepo:
                MockRepo.return_value = AsyncMock()
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as ac:
                    cb = await ac.get(
                        f"/api/auth/yahoo/callback?code=c&state={state}",
                        headers={"Cookie": f"{_NONCE_COOKIE}={nonce}"},
                    )
                assert cb.status_code == 302
                assert "platform=yahoo" in cb.headers.get("location", "")
                assert "error=" not in cb.headers.get("location", "")
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Callback — the happy path (valid signature + fresh + matching cookie)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_valid_flow_stores_tokens():
    """A signed, fresh state presented with the matching browser cookie succeeds
    and stores the tokens under the signed user id."""
    user = _make_user()
    from backend.core.dependencies import get_db

    mock_db = _mock_db_with_user(user)
    app.dependency_overrides[get_db] = lambda: mock_db

    state = _sign_state(str(user.id), _NONCE)
    mock_tokens = {
        "access_token": "new_access_token",
        "refresh_token": "new_refresh_token",
        "expires_in": 3600,
    }

    with patch(
        "backend.routers.auth.exchange_code_for_tokens",
        new_callable=AsyncMock,
        return_value=mock_tokens,
    ), patch("backend.routers.auth.CredentialRepository") as MockRepo:
        mock_repo = AsyncMock()
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=test_code&state={state}",
                    headers=_cookie(),
                )
            assert resp.status_code == 302
            assert "platform=yahoo" in resp.headers.get("location", "")
            assert "error=" not in resp.headers.get("location", "")

            mock_repo.upsert_yahoo.assert_called_once()
            call_kwargs = mock_repo.upsert_yahoo.call_args.kwargs
            assert call_kwargs.get("user_id") == user.id
            assert call_kwargs.get("access_token") == "new_access_token"
            assert call_kwargs.get("refresh_token") == "new_refresh_token"
            # The single-use binding cookie is cleared on success.
            assert _NONCE_COOKIE in resp.headers.get("set-cookie", "")
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_casts_user_id_to_uuid():
    """user_id from a verified state is cast to uuid.UUID before upsert."""
    user = _make_user()
    from backend.core.dependencies import get_db

    mock_db = _mock_db_with_user(user)
    app.dependency_overrides[get_db] = lambda: mock_db

    state = _sign_state(str(user.id), _NONCE)

    with patch(
        "backend.routers.auth.exchange_code_for_tokens",
        new_callable=AsyncMock,
        return_value={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    ), patch("backend.routers.auth.CredentialRepository") as MockRepo:
        mock_repo = AsyncMock()
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=c&state={state}",
                    headers=_cookie(),
                )
            assert resp.status_code == 302
            call_kwargs = mock_repo.upsert_yahoo.call_args.kwargs
            assert isinstance(call_kwargs["user_id"], uuid.UUID)
            assert call_kwargs["user_id"] == user.id
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Callback — rejection matrix (distinct error codes, no code exchange)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_missing_state_is_422():
    """state is a required query param — omitting it is a 422, not a redirect."""
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as ac:
            resp = await ac.get("/api/auth/yahoo/callback?code=abc")
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_tampered_state_rejects_invalid_signature():
    """A state whose signature does not verify → invalid_signature, no exchange."""
    user = _make_user()
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    valid = _sign_state(str(user.id), _NONCE)
    tampered = valid[:-2] + ("aa" if not valid.endswith("aa") else "bb")

    with patch(
        "backend.routers.auth.exchange_code_for_tokens", new_callable=AsyncMock,
    ) as mock_exchange:
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=c&state={tampered}",
                    headers=_cookie(),
                )
            assert resp.status_code == 302
            assert "error=invalid_signature" in resp.headers.get("location", "")
            mock_exchange.assert_not_called()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_non_base64_state_rejects_invalid_signature():
    """A garbage state (old unsigned/plain value) fails the HMAC check."""
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            resp = await ac.get(
                "/api/auth/yahoo/callback?code=abc&state=not-valid-signed-state!!!",
                headers=_cookie(),
            )
        assert resp.status_code == 302
        assert "error=invalid_signature" in resp.headers.get("location", "")
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_expired_state_rejects():
    """A validly-signed but stale state → expired_state, no exchange."""
    user = _make_user()
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    stale = _sign_state(str(user.id), _NONCE, ttl=-10)

    with patch(
        "backend.routers.auth.exchange_code_for_tokens", new_callable=AsyncMock,
    ) as mock_exchange:
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=c&state={stale}",
                    headers=_cookie(),
                )
            assert resp.status_code == 302
            assert "error=expired_state" in resp.headers.get("location", "")
            mock_exchange.assert_not_called()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_missing_cookie_rejects_missing_binding():
    """THE ATTACK: a state validly signed for user A, presented by a browser that
    carries NO binding cookie, is refused — the exchange never runs."""
    user_a = _make_user()
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    state = _sign_state(str(user_a.id), _NONCE)  # attacker's own valid state

    with patch(
        "backend.routers.auth.exchange_code_for_tokens", new_callable=AsyncMock,
    ) as mock_exchange, patch(
        "backend.routers.auth.CredentialRepository"
    ) as MockRepo:
        MockRepo.return_value = AsyncMock()
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                # No Cookie header — the victim's browser has no nonce.
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=victim_code&state={state}"
                )
            assert resp.status_code == 302
            assert "error=missing_binding" in resp.headers.get("location", "")
            mock_exchange.assert_not_called()
            MockRepo.return_value.upsert_yahoo.assert_not_called()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_mismatched_cookie_rejects_binding_mismatch():
    """A valid state whose signed nonce-hash does not match the presented cookie
    → binding_mismatch (attacker's state + a different browser's cookie)."""
    user = _make_user()
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    state = _sign_state(str(user.id), _NONCE)  # signed for _NONCE

    with patch(
        "backend.routers.auth.exchange_code_for_tokens", new_callable=AsyncMock,
    ) as mock_exchange:
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=c&state={state}",
                    headers=_cookie("a-different-browsers-nonce"),
                )
            assert resp.status_code == 302
            assert "error=binding_mismatch" in resp.headers.get("location", "")
            mock_exchange.assert_not_called()
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_non_uuid_user_id_rejects_invalid_state():
    """A signed+bound state whose user_id is not a UUID (only reachable with the
    signing key) is a defensive fallback → invalid_state, not a crash."""
    from backend.core.dependencies import get_db
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    state = _sign_state("not-a-uuid", _NONCE)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            resp = await ac.get(
                f"/api/auth/yahoo/callback?code=c&state={state}",
                headers=_cookie(),
            )
        assert resp.status_code == 302
        assert "error=invalid_state" in resp.headers.get("location", "")
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Callback — post-binding paths (user race, integrity error)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_missing_user_redirects_with_retry():
    """Binding passes but the user row is absent (Clerk webhook pending) →
    account_not_ready retry redirect, cookie cleared."""
    user = _make_user()
    from backend.core.dependencies import get_db

    mock_db = _mock_db_with_user(None)  # user NOT found
    app.dependency_overrides[get_db] = lambda: mock_db

    state = _sign_state(str(user.id), _NONCE)

    with patch(
        "backend.routers.auth.exchange_code_for_tokens",
        new_callable=AsyncMock,
        return_value={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    ):
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=c&state={state}",
                    headers=_cookie(),
                )
            assert resp.status_code == 302
            location = resp.headers.get("location", "")
            assert "error=account_not_ready" in location
            assert "retry=true" in location
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_callback_integrity_error_redirects_with_retry():
    """IntegrityError from upsert → graceful retry redirect, not a 500."""
    from sqlalchemy.exc import IntegrityError as SA_IntegrityError
    user = _make_user()
    from backend.core.dependencies import get_db

    mock_db = _mock_db_with_user(user)
    mock_db.rollback = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db

    state = _sign_state(str(user.id), _NONCE)

    with patch(
        "backend.routers.auth.exchange_code_for_tokens",
        new_callable=AsyncMock,
        return_value={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    ), patch("backend.routers.auth.CredentialRepository") as MockRepo:
        mock_repo = AsyncMock()
        mock_repo.upsert_yahoo.side_effect = SA_IntegrityError(
            "INSERT", {}, Exception("FK violation")
        )
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as ac:
                resp = await ac.get(
                    f"/api/auth/yahoo/callback?code=c&state={state}",
                    headers=_cookie(),
                )
            assert resp.status_code == 302
            assert "error=account_not_ready" in resp.headers.get("location", "")
        finally:
            app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Deleted endpoint — /yahoo/connect no longer exists
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_yahoo_connect_endpoint_removed():
    """The unauthenticatable top-level /yahoo/connect initiation was deleted."""
    user = _make_user()
    from backend.core.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as ac:
            resp = await ac.get("/api/auth/yahoo/connect")
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Unchanged surface — leagues / settings / disconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_yahoo_leagues_requires_connection():
    """GET /auth/yahoo/leagues returns 400 when Yahoo not connected."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    with patch("backend.routers.auth.CredentialRepository") as MockRepo:
        mock_repo = AsyncMock()
        mock_repo.get_yahoo_tokens.return_value = None
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as ac:
                resp = await ac.get("/api/auth/yahoo/leagues")
            assert resp.status_code == 400
            assert resp.json()["action"] == "connect"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_yahoo_leagues_returns_list():
    """GET /auth/yahoo/leagues returns league list when connected."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    future_expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)
    mock_leagues = [
        {"league_key": "449.l.123", "league_id": "123", "name": "Test League",
         "season": "2026", "num_teams": 12, "draft_type": "auction",
         "scoring_type": "head", "is_finished": False, "logo_url": ""},
    ]

    with patch("backend.routers.auth.CredentialRepository") as MockRepo, patch(
        "backend.routers.auth.get_user_leagues",
        new_callable=AsyncMock,
        return_value=mock_leagues,
    ):
        mock_repo = AsyncMock()
        mock_repo.get_yahoo_tokens.return_value = (
            "access_tok", "refresh_tok", future_expiry
        )
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as ac:
                resp = await ac.get("/api/auth/yahoo/leagues")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["leagues"]) == 1
            assert data["leagues"][0]["name"] == "Test League"
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_yahoo_leagues_auto_refreshes_expired_token():
    """GET /auth/yahoo/leagues refreshes token when expired."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    expired_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new_expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)

    with patch("backend.routers.auth.CredentialRepository") as MockRepo, patch(
        "backend.routers.auth.refresh_access_token_for_user",
        new_callable=AsyncMock,
        return_value=("new_access", "new_refresh", new_expiry),
    ) as mock_refresh, patch(
        "backend.routers.auth.get_user_leagues",
        new_callable=AsyncMock,
        return_value=[],
    ):
        mock_repo = AsyncMock()
        mock_repo.get_yahoo_tokens.return_value = (
            "old_access", "old_refresh", expired_at
        )
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as ac:
                resp = await ac.get("/api/auth/yahoo/leagues")
            assert resp.status_code == 200
            mock_refresh.assert_awaited_once_with("old_refresh")
            mock_repo.upsert_yahoo.assert_awaited_once_with(
                user.id, "new_access", "new_refresh", new_expiry,
            )
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_yahoo_league_settings_endpoint():
    """GET /auth/yahoo/league-settings returns settings for a league_key."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    mock_db = AsyncMock()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: mock_db

    future_expiry = datetime(2099, 1, 1, tzinfo=timezone.utc)
    mock_settings = {
        "name": "My League", "num_teams": 10, "draft_type": "auction",
        "scoring_type": "ppr", "auction_budget": 200,
        "trade_deadline": "2026-11-15", "waiver_type": "faab",
        "playoff_start_week": 14, "uses_faab": True,
    }

    with patch("backend.routers.auth.CredentialRepository") as MockRepo, patch(
        "backend.routers.auth.get_league_settings",
        new_callable=AsyncMock,
        return_value=mock_settings,
    ) as mock_get:
        mock_repo = AsyncMock()
        mock_repo.get_yahoo_tokens.return_value = (
            "access_tok", "refresh_tok", future_expiry
        )
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as ac:
                resp = await ac.get(
                    "/api/auth/yahoo/league-settings?league_key=470.l.12345"
                )
            assert resp.status_code == 200
            data = resp.json()
            assert data["settings"]["scoring_type"] == "ppr"
            mock_get.assert_awaited_once_with("access_tok", "470.l.12345")
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_league_settings_requires_league_key():
    """GET /auth/yahoo/league-settings without league_key returns 422."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as ac:
            resp = await ac.get("/api/auth/yahoo/league-settings")
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_yahoo_disconnect_removes_credentials():
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    with patch("backend.routers.auth.CredentialRepository") as MockRepo:
        mock_repo = AsyncMock()
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as ac:
                resp = await ac.delete("/api/auth/yahoo/disconnect")
            assert resp.status_code == 200
            assert resp.json()["status"] == "disconnected"
            mock_repo.disconnect.assert_called_once_with(user.id, "yahoo")
        finally:
            app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_espn_disconnect_removes_credentials():
    """ESPN disconnect mirrors Yahoo — same canonical repo.disconnect, platform='espn'."""
    user = _make_user()
    from backend.core.dependencies import get_current_user, get_db

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()

    with patch("backend.routers.auth.CredentialRepository") as MockRepo:
        mock_repo = AsyncMock()
        MockRepo.return_value = mock_repo
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test",
            ) as ac:
                resp = await ac.delete("/api/auth/espn/disconnect")
            assert resp.status_code == 200
            assert resp.json() == {"status": "disconnected", "platform": "espn"}
            mock_repo.disconnect.assert_called_once_with(user.id, "espn")
        finally:
            app.dependency_overrides.clear()
