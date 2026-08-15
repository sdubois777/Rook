"""Tests for backend/routers/email.py — the unsubscribe endpoint.

The router is mounted on a throwaway app here rather than on backend.main.app,
so these tests do not depend on where the parent wires it in. The database
session is a mock; nothing connects.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.config import settings
from backend.core.dependencies import get_db
from backend.routers import email as email_router
from backend.services.email.unsubscribe import make_token


def _oauth_style_state(
    user_id: str = "user_2abc", *, strip_padding: bool = False
) -> str:
    """An OAuth state value built the way backend/routers/auth.py builds one:
    b64url of a JSON payload, a dot, then HMAC-SHA256 of that payload under
    settings.secret_key. Reproduced rather than imported so this test still
    describes the real construction if the auth module moves.

    `strip_padding` removes the trailing "=", which read_token would reject on
    shape alone — so the rejection has to come from the purpose tag instead.
    """
    payload = {
        "user_id": user_id,
        "nh": hashlib.sha256(b"nonce").hexdigest(),
        "exp": int(time.time()) + 600,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    sig = hmac.new(
        settings.secret_key.encode(), body.encode(), hashlib.sha256
    ).hexdigest()
    return f"{body.rstrip('=')}.{sig}" if strip_padding else f"{body}.{sig}"


def _session(*, rowcount=1, error=None):
    session = AsyncMock()
    if error:
        session.execute.side_effect = error
    else:
        session.execute.return_value = MagicMock(rowcount=rowcount)
    return session


@pytest.fixture
def app_and_session():
    """A minimal app carrying only the email router."""
    session = _session()
    app = FastAPI()
    app.include_router(email_router.router, prefix="/api")

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    return app, session


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestUnsubscribeGet:
    async def test_valid_token_suppresses_the_address_and_returns_a_page(
        self, app_and_session
    ):
        app, session = app_and_session
        token = make_token("sam@example.com")

        async with await _client(app) as client:
            resp = await client.get(f"/api/email/unsubscribe?token={token}")

        assert resp.status_code == 200
        assert "unsubscribed" in resp.text.lower()
        session.execute.assert_awaited_once()
        session.commit.assert_awaited_once()

        # The suppression row is written for the lowercased address in the token.
        stmt = session.execute.await_args.args[0]
        params = stmt.compile().params
        assert params["email"] == "sam@example.com"
        assert params["reason"] == "unsubscribe"

    async def test_mixed_case_address_is_suppressed_lowercased(
        self, app_and_session
    ):
        app, session = app_and_session
        token = make_token("SAM@Example.COM")

        async with await _client(app) as client:
            await client.get(f"/api/email/unsubscribe?token={token}")

        params = session.execute.await_args.args[0].compile().params
        assert params["email"] == "sam@example.com"

    async def test_second_click_is_idempotent(self, app_and_session):
        """ON CONFLICT DO NOTHING => rowcount 0, and the page still confirms."""
        app, session = app_and_session
        session.execute.return_value = MagicMock(rowcount=0)
        token = make_token("sam@example.com")

        async with await _client(app) as client:
            resp = await client.get(f"/api/email/unsubscribe?token={token}")

        assert resp.status_code == 200
        assert "unsubscribed" in resp.text.lower()

    async def test_invalid_token_returns_a_friendly_page_and_writes_nothing(
        self, app_and_session
    ):
        app, session = app_and_session

        async with await _client(app) as client:
            resp = await client.get("/api/email/unsubscribe?token=garbage.abc")

        assert resp.status_code == 200
        assert "did not work" in resp.text.lower()
        assert "Traceback" not in resp.text
        session.execute.assert_not_awaited()

    @pytest.mark.parametrize(
        "raw_token",
        [
            "abc.%C3%A9",              # the reported trigger: a non-ASCII sig
            "%C3%A9.abc",              # non-ASCII body
            "abc.%00",                 # NUL in the signature
            "%E6%97%A5%E6%9C%AC.%E8%AA%9E",
        ],
    )
    async def test_non_ascii_token_renders_the_page_instead_of_a_500(
        self, app_and_session, raw_token
    ):
        """FIX 1. hmac.compare_digest raises TypeError on a non-ASCII str, and
        the read_token call sat outside every try, so this was an
        unauthenticated 500 that anyone could trigger by hand."""
        app, session = app_and_session

        async with await _client(app) as client:
            resp = await client.get(f"/api/email/unsubscribe?token={raw_token}")

        assert resp.status_code == 200
        assert "did not work" in resp.text.lower()
        assert "Traceback" not in resp.text
        session.execute.assert_not_awaited()

    @pytest.mark.parametrize("strip_padding", [False, True])
    async def test_an_oauth_shaped_state_token_does_not_unsubscribe_anyone(
        self, app_and_session, strip_padding
    ):
        """FIX 2. The OAuth state construction and the unsubscribe construction
        used the same key and the same format, so tokens were interchangeable in
        both directions. A state value must not suppress an address."""
        app, session = app_and_session
        state = _oauth_style_state(strip_padding=strip_padding)

        async with await _client(app) as client:
            resp = await client.get(f"/api/email/unsubscribe?token={state}")

        assert resp.status_code == 200
        assert "did not work" in resp.text.lower()
        session.execute.assert_not_awaited()

    async def test_a_read_token_failure_renders_the_page_rather_than_a_500(
        self, app_and_session, monkeypatch
    ):
        """read_token is written never to raise. The handler does not rely on
        that: a future change inside it must not become an unauthenticated 500."""
        app, session = app_and_session
        monkeypatch.setattr(
            email_router,
            "read_token",
            MagicMock(side_effect=TypeError("comparing strings requires ASCII")),
        )

        async with await _client(app) as client:
            resp = await client.get("/api/email/unsubscribe?token=anything.abc")

        assert resp.status_code == 200
        assert "did not work" in resp.text.lower()
        session.execute.assert_not_awaited()

    async def test_missing_token_returns_a_friendly_page(self, app_and_session):
        app, session = app_and_session

        async with await _client(app) as client:
            resp = await client.get("/api/email/unsubscribe")

        assert resp.status_code == 200
        assert "did not work" in resp.text.lower()
        session.execute.assert_not_awaited()

    async def test_page_never_echoes_the_address(self, app_and_session):
        """A leaked link must not disclose the address to whoever opens it."""
        app, _ = app_and_session
        token = make_token("sam@example.com")

        async with await _client(app) as client:
            resp = await client.get(f"/api/email/unsubscribe?token={token}")

        assert "sam@example.com" not in resp.text

    async def test_response_is_html_and_not_cached(self, app_and_session):
        app, _ = app_and_session
        token = make_token("sam@example.com")

        async with await _client(app) as client:
            resp = await client.get(f"/api/email/unsubscribe?token={token}")

        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["cache-control"] == "no-store"

    async def test_database_failure_returns_an_error_page_not_a_traceback(self):
        app = FastAPI()
        app.include_router(email_router.router, prefix="/api")
        session = _session(error=RuntimeError("connection reset"))

        async def _override():
            yield session

        app.dependency_overrides[get_db] = _override
        token = make_token("sam@example.com")

        async with await _client(app) as client:
            resp = await client.get(f"/api/email/unsubscribe?token={token}")

        assert resp.status_code == 500
        assert "went wrong" in resp.text.lower()
        assert "connection reset" not in resp.text
        assert "Traceback" not in resp.text


class TestUnsubscribePost:
    async def test_one_click_post_suppresses_and_returns_200(
        self, app_and_session
    ):
        """RFC 8058: the mail client POSTs List-Unsubscribe=One-Click."""
        app, session = app_and_session
        token = make_token("sam@example.com")

        async with await _client(app) as client:
            resp = await client.post(
                f"/api/email/unsubscribe?token={token}",
                content="List-Unsubscribe=One-Click",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        assert resp.status_code == 200
        session.commit.assert_awaited_once()

    async def test_post_with_an_invalid_token_still_returns_a_page(
        self, app_and_session
    ):
        app, session = app_and_session

        async with await _client(app) as client:
            resp = await client.post("/api/email/unsubscribe?token=nope")

        assert resp.status_code == 200
        session.execute.assert_not_awaited()
