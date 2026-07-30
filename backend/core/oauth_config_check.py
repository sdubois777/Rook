"""Startup validation that an OAuth redirect URI points at a route that exists.

WHY THIS EXISTS. `YAHOO_REDIRECT_URI` was configured as
``https://localhost:8000/auth/yahoo/callback`` while every router is mounted under
``/api`` — so the real handler lives at ``/api/auth/yahoo/callback``.

The failure was completely silent, and worse than a 404 would have been: the SPA
catch-all answers unmatched GETs with ``200`` and index.html, so Yahoo redirected the
browser back with ``?code=…``, the frontend rendered, and the token exchange never ran.
No error anywhere — the connection simply never completed, and the same misconfiguration
sat in the Yahoo developer console's registered URI list for two of three environments.

A path that resolves to the SPA is indistinguishable from a working one by status code,
which is exactly why this has to be checked against the ROUTE TABLE rather than by
probing the URL.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def find_callback_mismatch(
    redirect_uri: str | None,
    route_paths: list[str],
    *,
    suffix: str = "/auth/yahoo/callback",
) -> str | None:
    """Return a human-readable problem with `redirect_uri`, or None if it is fine.

    Args:
        redirect_uri: the configured OAuth redirect (may be None/blank — not our problem
            here, the missing-settings check already covers that).
        route_paths: every path the app actually serves, e.g. from `app.routes`.
        suffix: the un-prefixed callback path to look for.

    Compares against the mounted route table, not against a hardcoded expectation, so it
    stays correct if the `/api` prefix ever moves.
    """
    if not redirect_uri:
        return None

    configured = urlparse(redirect_uri).path.rstrip("/")
    if not configured:
        return f"YAHOO_REDIRECT_URI has no path: {redirect_uri!r}"

    matches = [p for p in route_paths if p.rstrip("/").endswith(suffix)]
    if not matches:
        # The route isn't mounted at all; nothing to compare against.
        return None

    if any(configured == p.rstrip("/") for p in matches):
        return None

    return (
        f"YAHOO_REDIRECT_URI path {configured!r} does not match the mounted callback "
        f"route {matches[0]!r}. Yahoo will redirect the browser to a path this app does "
        f"not handle — the SPA catch-all answers it with 200 and index.html, so the "
        f"token exchange silently never runs and the connection never completes. "
        f"Set YAHOO_REDIRECT_URI to end in {matches[0]!r} AND register that exact URI "
        f"in the Yahoo developer console."
    )


def check_oauth_redirects(app, redirect_uri: str | None) -> str | None:
    """Log the mismatch (if any) at ERROR and return it. Never raises.

    Deliberately a warning rather than a hard failure: a wrong redirect URI breaks
    connecting a Yahoo league, not the whole app, and refusing to boot over it would take
    down every unrelated surface.
    """
    paths = [getattr(r, "path", "") for r in getattr(app, "routes", [])]
    problem = find_callback_mismatch(redirect_uri, paths)
    if problem:
        logger.error("OAUTH REDIRECT MISCONFIGURED — %s", problem)
    return problem
