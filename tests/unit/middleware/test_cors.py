"""Tests for the CORS configuration in backend/main.py.

The browser extension calls the API from moz-extension:// and
chrome-extension:// origins (per-install random IDs), so preflight
must succeed for those origins and the X-Draft-Token header.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


async def _preflight(origin: str, request_headers: str = "x-draft-token"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.options(
            "/api/leagues/sync-platform/yahoo",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": request_headers,
            },
        )


@pytest.mark.asyncio
async def test_cors_allows_extension_origins():
    """Preflight from Firefox and Chrome extension origins succeeds."""
    for origin in (
        "moz-extension://4f8a02f1-aaaa-bbbb-cccc-1234567890ab",
        "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    ):
        resp = await _preflight(origin)
        assert resp.status_code == 200, f"preflight rejected for {origin}"
        assert resp.headers["access-control-allow-origin"] == origin


@pytest.mark.asyncio
async def test_cors_allows_x_draft_token_header():
    """Preflight requesting X-Draft-Token (and the other required headers) succeeds."""
    resp = await _preflight(
        "moz-extension://4f8a02f1-aaaa-bbbb-cccc-1234567890ab",
        request_headers="x-draft-token, authorization, content-type",
    )

    assert resp.status_code == 200
    allowed = resp.headers["access-control-allow-headers"].lower()
    assert "x-draft-token" in allowed
    assert "authorization" in allowed
    assert "content-type" in allowed


@pytest.mark.asyncio
async def test_cors_still_allows_web_origins():
    """The dev server and production web origins keep working after the regex switch."""
    for origin in (
        "http://localhost:5173",
        "http://localhost:3000",
        "https://fantasymanager-production.up.railway.app",
    ):
        resp = await _preflight(origin, request_headers="authorization")
        assert resp.status_code == 200, f"preflight rejected for {origin}"
        assert resp.headers["access-control-allow-origin"] == origin


@pytest.mark.asyncio
async def test_cors_allows_rookff_custom_domain():
    """The Rook custom domain (apex + www) is allowed."""
    for origin in ("https://rookff.com", "https://www.rookff.com"):
        resp = await _preflight(origin, request_headers="authorization")
        assert resp.status_code == 200, f"preflight rejected for {origin}"
        assert resp.headers["access-control-allow-origin"] == origin


@pytest.mark.asyncio
async def test_cors_allows_espn_draft_origin():
    """The ESPN draft site (where the extension's ESPN poller runs) is allowed."""
    resp = await _preflight("https://fantasy.espn.com", request_headers="x-draft-token")
    assert resp.status_code == 200, "preflight rejected for https://fantasy.espn.com"
    assert resp.headers["access-control-allow-origin"] == "https://fantasy.espn.com"


@pytest.mark.asyncio
async def test_cors_rejects_unknown_origin():
    """Arbitrary web origins are still refused."""
    resp = await _preflight("https://evil.example.com")
    assert "access-control-allow-origin" not in resp.headers
    # A lookalike must not slip through the rookff.com alternative.
    resp = await _preflight("https://notrookff.com.evil.com")
    assert "access-control-allow-origin" not in resp.headers
    # ...nor a lookalike of the ESPN alternative (fantasy.espn.com.evil.com).
    resp = await _preflight("https://fantasy.espn.com.evil.com")
    assert "access-control-allow-origin" not in resp.headers
