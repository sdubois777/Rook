"""Tests for frontend static file serving from FastAPI."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app, FRONTEND_DIST

# These two cases assert the built SPA is served. The backend CI job does not
# build the frontend (that is the frontend job), so skip them when the artifact
# is absent rather than asserting against a build that isn't there.
needs_frontend_build = pytest.mark.skipif(
    not (FRONTEND_DIST / "index.html").exists(),
    reason="frontend/dist/index.html not built",
)


@needs_frontend_build
@pytest.mark.asyncio
async def test_frontend_served_at_root():
    """GET / returns HTML not JSON when frontend/dist exists."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_api_routes_not_intercepted():
    """API routes still return JSON, not index.html."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


@needs_frontend_build
@pytest.mark.asyncio
async def test_unknown_path_serves_frontend():
    """Any unknown non-API path serves index.html for React Router."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/some/nested/route")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


@needs_frontend_build
@pytest.mark.asyncio
async def test_index_html_served_with_no_cache_headers():
    """The SPA shell must be served no-cache so browsers never keep a stale
    bundle reference after a deploy (the Firefox stale-bundle bug)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/", "/some/nested/route"):
            resp = await client.get(path)
            assert resp.status_code == 200
            cc = resp.headers.get("cache-control", "")
            assert "no-cache" in cc and "no-store" in cc, f"{path}: {cc!r}"


@pytest.mark.asyncio
async def test_health_includes_version():
    """Health check includes version field."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        data = resp.json()
        assert "version" in data
