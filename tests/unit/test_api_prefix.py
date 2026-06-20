"""All API routers are mounted under /api so the root namespace is free for the
SPA (React Router paths like /draftboard, /account no longer collide with
same-named API routers). webhooks stays at /webhooks; /health stays at root.

These tests don't require a built frontend/dist: they assert route registration
and that the SPA paths NO LONGER return API JSON (the bug). The exact HTML body
is only asserted when a dist happens to be present.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.routing import WebSocketRoute

from backend.main import app


def _paths() -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


def test_api_routes_under_api_prefix():
    paths = _paths()
    assert any(p and p.startswith("/api/draftboard") for p in paths)
    assert any(p and p.startswith("/api/players") for p in paths)
    assert any(p and p.startswith("/api/account") for p in paths)
    assert any(p and p.startswith("/api/draft") for p in paths)


def test_api_routers_no_longer_at_root():
    paths = _paths()
    # The old root API routes are gone (only the SPA catch-all lives at root now).
    assert "/draftboard" not in paths
    assert "/players" not in paths
    assert "/account/me" not in paths


def test_webhooks_and_health_stay_at_root():
    paths = _paths()
    assert any(p and p.startswith("/webhooks") for p in paths)
    assert not any(p and p.startswith("/api/webhooks") for p in paths)
    assert "/health" in paths


def test_websocket_connects_under_api():
    ws_paths = {r.path for r in app.routes if isinstance(r, WebSocketRoute)}
    assert "/api/draft/ws/draft" in ws_paths   # moved under /api with the router
    assert "/ws/news" in ws_paths               # app-level news WS stays at root


@pytest.mark.asyncio
async def test_health_works():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def _get(path: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(path)


async def _assert_spa_not_api(path: str):
    """A SPA path must serve index.html (200 text/html, no-cache) when a dist is
    built, or a plain 404 when it isn't (FastAPI's default 404 is JSON — that's
    fine, it's "not found", not the API's data). It must NEVER be a 200 with API
    JSON, which was the bug."""
    resp = await _get(path)
    ctype = resp.headers.get("content-type", "")
    assert not (resp.status_code == 200 and "application/json" in ctype), (
        f"{path} returned a 200 API JSON response instead of the SPA shell"
    )
    if resp.status_code == 200:
        assert "text/html" in ctype
        assert "no-cache" in resp.headers.get("cache-control", "").lower()
    else:
        assert resp.status_code == 404  # no built frontend in this env


@pytest.mark.asyncio
async def test_draftboard_refresh_returns_html():
    await _assert_spa_not_api("/draftboard")


@pytest.mark.asyncio
async def test_account_refresh_returns_html():
    await _assert_spa_not_api("/account")


@pytest.mark.asyncio
async def test_news_refresh_returns_html():
    await _assert_spa_not_api("/news")


@pytest.mark.asyncio
async def test_spa_catch_all_not_hit_by_api():
    # An /api path that doesn't exist must 404 (the catch-all must NOT serve the
    # SPA shell for /api/*), so the frontend never gets HTML where it expects JSON.
    resp = await _get("/api/this-route-does-not-exist")
    assert resp.status_code == 404
    assert "text/html" not in resp.headers.get("content-type", "")