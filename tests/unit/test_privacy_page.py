"""The public /privacy page — Chrome Web Store hard requirement.

Must be 200 with full content for an UNAUTHENTICATED, non-JS request, and must NOT
redirect (the store review bot crawls it; a 404/redirect/placeholder auto-rejects).
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


def _client() -> AsyncClient:
    # follow_redirects=False so a stray redirect fails loudly instead of hiding.
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       follow_redirects=False)


@pytest.mark.asyncio
async def test_privacy_public_200_with_content_no_auth():
    async with _client() as ac:
        resp = await ac.get("/privacy")  # no Authorization header at all
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert not resp.is_redirect
    html = resp.text
    # Full policy content is in the RAW response (server-rendered — no JS needed).
    assert "Rook Privacy Policy" in html
    assert "Last updated" in html          # the required visible date
    assert "<table>" in html               # the permissions table survived rendering
    assert "espn_s2" in html               # real data-flow content, not a placeholder


@pytest.mark.asyncio
async def test_privacy_html_alias_200():
    async with _client() as ac:
        resp = await ac.get("/privacy.html")
    assert resp.status_code == 200
    assert "Rook Privacy Policy" in resp.text


@pytest.mark.asyncio
async def test_privacy_not_the_spa_shell():
    """/privacy must be the policy page, not the SPA index shell (which would only
    render client-side and could redirect a signed-out user to /dashboard)."""
    async with _client() as ac:
        resp = await ac.get("/privacy")
    # The SPA shell has a <div id="root">/Vite bundle; the policy page has the table.
    assert "<table>" in resp.text
    assert 'id="root"' not in resp.text
