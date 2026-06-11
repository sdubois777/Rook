"""Tests for backend/middleware/security_headers.py"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


@pytest.mark.asyncio
async def test_security_headers_present_on_every_response():
    """All hardening headers are set on a normal API response."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 200
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "Content-Security-Policy" in resp.headers
    assert "server" not in resp.headers
    assert "x-powered-by" not in resp.headers


@pytest.mark.asyncio
async def test_csp_skipped_for_docs():
    """/docs needs inline Swagger assets, so CSP is not applied there."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/docs")

    assert "Content-Security-Policy" not in resp.headers
