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

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app

UNPREFIXED = "/auth/yahoo/callback"
PREFIXED = "/api/auth/yahoo/callback"

_MAIN_PY = Path(__file__).resolve().parents[2] / "backend" / "main.py"


def _paths() -> list[str]:
    return [getattr(r, "path", "") for r in app.routes]


def test_both_callback_paths_are_registered():
    paths = _paths()
    assert PREFIXED in paths
    assert UNPREFIXED in paths


def test_the_alias_is_defined_before_the_spa_catch_all_in_source():
    """The ordering guard that survives a backend-only CI job.

    The runtime check below cannot run there: the catch-all is registered inside
    ``if FRONTEND_DIST.exists()`` (backend/main.py), and CI's backend job never builds
    the frontend — so the route simply is not on ``app`` and the ordering is unobservable
    at runtime. Asserting on source order keeps the invariant guarded in exactly the
    place a regression would otherwise sail through.
    """
    src = _MAIN_PY.read_text(encoding="utf-8")
    alias = src.index('@app.get("/auth/yahoo/callback"')
    catch_all = src.index('"/{full_path:path}"')
    assert alias < catch_all, (
        "The Yahoo callback alias must be defined before the SPA catch-all. After it, "
        "the catch-all answers /auth/yahoo/callback with 200 + index.html and the token "
        "exchange silently never runs — the exact regression fba1f78 shipped."
    )


def test_the_alias_is_defined_before_the_spa_catch_all():
    """Same invariant, checked against the live route table where the SPA is served."""
    paths = _paths()
    catch_all = next((i for i, p in enumerate(paths) if "{full_path" in p), None)
    if catch_all is None:
        pytest.skip(
            "SPA catch-all not registered — frontend/dist is absent (backend-only run). "
            "Source-order equivalent above still guards this."
        )
    assert paths.index(UNPREFIXED) < catch_all


def test_the_prefixed_path_reaches_the_real_handler():
    """422 means the OAuth handler ran and rejected a request with no code/state.
    200 would mean the SPA catch-all swallowed it — the original silent failure."""
    r = TestClient(app).get(PREFIXED)
    assert r.status_code == 422


def test_the_alias_redirects_rather_than_handling_in_place():
    """It MUST bounce to the canonical path, not serve the callback itself.

    The single-use binding nonce is set with Path=/api/auth/yahoo, and a cookie is not
    sent to a path outside its scope — so handling the callback at the un-prefixed path
    meant the browser arrived without it and the flow died on `missing_binding`. The
    redirect lets the cookie attach on the follow-up request, keeping the nonce narrowly
    scoped rather than widening it to "/".
    """
    r = TestClient(app, follow_redirects=False).get(UNPREFIXED)
    assert r.status_code == 307
    assert r.headers["location"] == PREFIXED


def test_the_alias_carries_code_and_state_through():
    """Dropping the query would turn a working consent into a silent failure."""
    r = TestClient(app, follow_redirects=False).get(
        UNPREFIXED, params={"code": "abc123", "state": "signed.state"})
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith(PREFIXED + "?")
    assert "code=abc123" in loc
    assert "state=signed.state" in loc


def test_following_the_alias_lands_on_the_real_handler():
    r = TestClient(app).get(UNPREFIXED)
    assert r.status_code == 422      # the handler ran, not the SPA


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
