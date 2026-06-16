"""Guard tests for the /api prefix routing.

The frontend axios client prefixes every request with /api, and in production
there is no proxy to strip it — so FastAPI must serve frontend routers under
/api. Dual-consumer routers (draft, auth, league_connect) are ALSO mounted bare
because the extension / WebSocket / Yahoo / Clerk reach them at bare paths.
These tests pin that wiring so a future edit can't silently break it.
"""
from __future__ import annotations

from backend.main import app


def _paths() -> set[str]:
    return {getattr(r, "path", None) for r in app.routes}


def _has_prefix(prefix: str) -> bool:
    return any(p and p.startswith(prefix) for p in _paths())


def test_frontend_routers_served_under_api():
    paths = _paths()
    assert "/api/draftboard" in paths
    assert "/api/players" in paths or _has_prefix("/api/players")
    # bare path no longer routed (frontend-only router moved under /api)
    assert "/draftboard" not in paths


def test_health_stays_at_root_not_under_api():
    paths = _paths()
    assert "/health" in paths
    assert "/api/health" not in paths


def test_draft_is_dual_mounted():
    paths = _paths()
    # extension relay + WebSocket must keep working at bare paths
    assert "/draft/event" in paths
    assert "/draft/ws/draft" in paths
    # frontend reaches the HTTP actions under /api too
    assert "/api/draft/event" in paths
    assert "/api/draft/ws/draft" in paths


def test_auth_callback_kept_bare_and_connect_under_api():
    paths = _paths()
    # Yahoo's registered redirect_uri is the bare path
    assert "/auth/yahoo/callback" in paths
    # frontend initiates the flow under /api
    assert "/api/auth/yahoo/callback" in paths


def test_leagues_dual_mounted_for_extension():
    # extension POSTs bare /leagues/sync-platform and /leagues/connect/espn/callback
    assert _has_prefix("/leagues/sync-platform")
    assert _has_prefix("/leagues/connect/espn")
    # frontend reaches them under /api
    assert _has_prefix("/api/leagues/sync-platform")


def test_webhooks_bare_only():
    # Clerk posts directly to the bare path; the frontend never calls it
    assert _has_prefix("/webhooks")
    assert not _has_prefix("/api/webhooks")
