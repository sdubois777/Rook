"""The production SPA server must serve real root static files — favicons, the
web manifest, og-image, the mascot — with correct content-types, NOT the
index.html shell.

Regression: Vite copies frontend/public/* to the dist ROOT (not /assets), but
only /favicon.svg and /icons.svg had explicit routes, so /site.webmanifest and
/rook-mascot.png fell through to the catch-all and were served index.html. That
HTML made the manifest fail to parse ("Manifest: Line 1, column 1, Syntax
error") and rendered the hero <img src="/rook-mascot.png"> broken.

Skipped unless the frontend has been built — the static routes only mount when
frontend/dist exists (so the backend-only CI job, which doesn't build the
frontend, skips rather than fails).
"""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import FRONTEND_DIST, app

pytestmark = pytest.mark.skipif(
    not FRONTEND_DIST.exists(), reason="frontend/dist not built"
)


async def _get(path: str):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        return await ac.get(path)


@pytest.mark.asyncio
async def test_webmanifest_served_as_manifest_json_not_html():
    resp = await _get("/site.webmanifest")
    assert resp.status_code == 200
    assert "manifest+json" in resp.headers["content-type"]
    # Parses as JSON — would raise (and the bug returned) if it were index.html.
    assert json.loads(resp.text)["name"] == "Rook"


@pytest.mark.asyncio
async def test_root_pngs_served_as_images_not_html():
    for path in ("/rook-mascot.png", "/og-image.png", "/apple-touch-icon.png"):
        resp = await _get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"] == "image/png", path


@pytest.mark.asyncio
async def test_spa_route_still_serves_html_shell():
    resp = await _get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert 'id="root"' in resp.text


@pytest.mark.asyncio
async def test_unknown_root_path_falls_back_to_shell():
    # Any path that isn't a real file is a client-side route → the SPA shell.
    resp = await _get("/not-a-real-asset.png")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
