"""The public /terms page — mirrors /privacy.

Must be 200 with full content for an UNAUTHENTICATED, non-JS request, and must NOT
redirect or fall through to the SPA shell. Assertions stay on stable markers (title,
server-rendered structure) so they hold whatever verbatim Terms document is published.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       follow_redirects=False)


@pytest.mark.asyncio
async def test_terms_public_200_with_content_no_auth():
    async with _client() as ac:
        resp = await ac.get("/terms")  # no Authorization header at all
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert not resp.is_redirect
    html = resp.text
    # Server-rendered — the content is in the RAW response, no JS needed.
    assert "Terms of Service" in html
    assert "<h1" in html                    # a real rendered document, not blank


@pytest.mark.asyncio
async def test_terms_html_alias_200():
    async with _client() as ac:
        resp = await ac.get("/terms.html")
    assert resp.status_code == 200
    assert "Terms of Service" in resp.text


@pytest.mark.asyncio
async def test_terms_not_the_spa_shell():
    """/terms must be the policy page, not the SPA index shell (which renders only
    client-side and could redirect a signed-out user to /dashboard)."""
    async with _client() as ac:
        resp = await ac.get("/terms")
    assert 'id="root"' not in resp.text
