"""The redirect-URI/route-table check.

The bug it exists to catch cost a long debugging session and looked like a Yahoo outage:
YAHOO_REDIRECT_URI was ``https://localhost:8000/auth/yahoo/callback`` while the routers
mount under ``/api``. Because the SPA catch-all answers unmatched GETs with 200 and
index.html, the wrong path was indistinguishable from the right one by status code — the
browser came back from Yahoo, the app rendered, and the token exchange silently never ran.
"""
from __future__ import annotations

import logging

from backend.core.oauth_config_check import check_oauth_redirects, find_callback_mismatch

ROUTES = [
    "/health",
    "/api/auth/yahoo/callback",
    "/api/auth/yahoo/connect-url",
    "/{full_path:path}",          # the SPA catch-all that made this silent
]


def test_matching_path_is_accepted():
    assert find_callback_mismatch(
        "https://rookff.com/api/auth/yahoo/callback", ROUTES) is None


def test_the_real_bug_is_caught():
    problem = find_callback_mismatch(
        "https://localhost:8000/auth/yahoo/callback", ROUTES)
    assert problem is not None
    assert "/api/auth/yahoo/callback" in problem       # names the correct path
    assert "silently" in problem                        # explains WHY it looked fine


def test_the_message_says_to_update_the_yahoo_console_too():
    """Fixing only the env var leaves the console's registered URI wrong, and Yahoo
    rejects a redirect_uri that isn't registered — so the message must say both."""
    problem = find_callback_mismatch("https://x.com/auth/yahoo/callback", ROUTES)
    assert "developer console" in problem


def test_trailing_slash_is_not_a_mismatch():
    assert find_callback_mismatch(
        "https://rookff.com/api/auth/yahoo/callback/", ROUTES) is None


def test_railway_host_with_the_wrong_path_is_caught():
    # One of the three URIs registered in the console, and broken the same way.
    assert find_callback_mismatch(
        "https://fantasymanager-production.up.railway.app/auth/yahoo/callback",
        ROUTES) is not None


def test_a_different_host_is_not_flagged():
    """Only the PATH is checked. Host/scheme differ legitimately per environment
    (localhost vs Railway vs rookff.com) and are the console's business, not ours."""
    assert find_callback_mismatch(
        "https://any-host.example/api/auth/yahoo/callback", ROUTES) is None


def test_no_redirect_configured_is_not_an_error():
    # The missing-settings check already covers absence; don't double-report.
    assert find_callback_mismatch(None, ROUTES) is None
    assert find_callback_mismatch("", ROUTES) is None


def test_uri_without_a_path_is_reported():
    assert find_callback_mismatch("https://rookff.com", ROUTES) is not None


def test_no_opinion_when_the_route_is_not_mounted():
    """If the callback route doesn't exist there is nothing to compare against — stay
    quiet rather than emit a confident wrong answer."""
    assert find_callback_mismatch(
        "https://rookff.com/whatever", ["/health"]) is None


def test_check_logs_at_error_and_returns_the_problem(caplog):
    class _App:
        routes = [type("R", (), {"path": p})() for p in ROUTES]

    with caplog.at_level(logging.ERROR, logger="backend.core.oauth_config_check"):
        problem = check_oauth_redirects(_App(), "https://localhost:8000/auth/yahoo/callback")
    assert problem is not None
    assert any("OAUTH REDIRECT MISCONFIGURED" in r.getMessage() for r in caplog.records)


def test_check_is_silent_when_correct(caplog):
    class _App:
        routes = [type("R", (), {"path": p})() for p in ROUTES]

    with caplog.at_level(logging.ERROR, logger="backend.core.oauth_config_check"):
        assert check_oauth_redirects(_App(), "https://rookff.com/api/auth/yahoo/callback") is None
    assert not caplog.records


def test_check_never_raises_on_a_malformed_app():
    """It runs at startup; a bug here must not take the whole app down."""
    assert check_oauth_redirects(object(), "https://x/api/auth/yahoo/callback") is None
