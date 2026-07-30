"""The Yahoo callback must be reachable at BOTH the /api path and the un-prefixed one.

The redirect URI is an external contract: Yahoo only redirects to a URI already registered
in its developer console, and the registered localhost/Railway entries predate the /api
prefix. Yahoo rejects anything else outright —

    302 -> /oauth2/error?error=invalid_request&error_description=invalid+redirect+uri

— so the app has to meet the registered path where it already is.

The ordering matters more than it looks: routes match in definition order and the SPA
catch-all answers ANY unmatched GET with 200 and index.html. Before the alias existed,
/auth/yahoo/callback returned 200 and the frontend rendered, so Yahoo's ?code= landed on a
page that ignored it and the token exchange silently never ran. A regression here would not
throw — it would go quiet, which is why the 200-vs-422 distinction is asserted directly.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app

UNPREFIXED = "/auth/yahoo/callback"
PREFIXED = "/api/auth/yahoo/callback"


def _paths() -> list[str]:
    return [getattr(r, "path", "") for r in app.routes]


def test_both_callback_paths_are_registered():
    paths = _paths()
    assert PREFIXED in paths
    assert UNPREFIXED in paths


def test_the_alias_is_defined_before_the_spa_catch_all():
    """Order is the whole mechanism — after the catch-all the alias is unreachable."""
    paths = _paths()
    catch_all = next(i for i, p in enumerate(paths) if "{full_path" in p)
    assert paths.index(UNPREFIXED) < catch_all


def test_both_paths_reach_the_real_handler_not_the_spa():
    """422 means the OAuth handler ran and rejected a request with no code/state.
    200 would mean the SPA catch-all swallowed it — the original silent failure."""
    client = TestClient(app)
    for path in (UNPREFIXED, PREFIXED):
        r = client.get(path)
        assert r.status_code == 422, f"{path} returned {r.status_code}, expected 422"


def test_both_paths_share_one_handler():
    """Registered from the same function object, so the two cannot drift apart."""
    endpoints = {
        r.endpoint for r in app.routes
        if getattr(r, "path", "") in (UNPREFIXED, PREFIXED)
    }
    assert len(endpoints) == 1


def test_the_alias_is_hidden_from_the_public_schema():
    alias = next(r for r in app.routes if getattr(r, "path", "") == UNPREFIXED)
    assert alias.include_in_schema is False


def test_the_startup_check_is_satisfied_by_either_path():
    """The redirect-URI guard reads the route table, so registering the alias is what
    makes the un-prefixed env value legitimate rather than something to warn about."""
    from backend.core.oauth_config_check import find_callback_mismatch

    paths = _paths()
    assert find_callback_mismatch(f"https://localhost:8000{UNPREFIXED}", paths) is None
    assert find_callback_mismatch(f"https://rookff.com{PREFIXED}", paths) is None
    # …and something that matches neither is still caught.
    assert find_callback_mismatch("https://x/auth/callback", paths) is not None
