"""Yahoo auth refusals must be explained, not crash.

A live 403 from Yahoo ("This application is not authorized to perform this action")
propagated out of httpx's raise_for_status() as an unhandled 500 with a stack trace. Three
very different situations looked identical — a dead user grant, a revoked token, and Rook's
own developer app losing its Fantasy Sports permission — and only the first two are fixed
by the "reconnect" the UI would otherwise suggest.
"""
from __future__ import annotations

import httpx
import pytest

from backend.integrations.yahoo_api import (
    YahooAuthError,
    _api_get_with_token,
    _raise_for_yahoo_auth,
)


def _resp(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body if body is not None else {},
        request=httpx.Request("GET", "https://fantasysports.yahooapis.com/x"),
    )


APP_403 = {
    "error": {
        "xml:lang": "en-us",
        "description": "This application is not authorized to perform this action.",
        "detail": "",
    }
}


# ---------------------------------------------------------------------------
# 403 — Rook's app, not the user's account
# ---------------------------------------------------------------------------

def test_403_raises_a_502_that_does_not_blame_the_user():
    with pytest.raises(YahooAuthError) as exc:
        _raise_for_yahoo_auth(_resp(403, APP_403), "users;use_login=1/games")
    err = exc.value
    assert err.status_code == 502          # upstream refusal, not a client error
    assert err.detail["yahoo_status"] == 403
    # The remedy differs from the 401 case, so the message must NOT ask them to reconnect.
    assert "reconnect" not in err.message.lower() or "not help" in err.message.lower()


def test_403_carries_yahoos_own_description_through():
    with pytest.raises(YahooAuthError) as exc:
        _raise_for_yahoo_auth(_resp(403, APP_403), "game/nfl")
    assert "not authorized" in exc.value.detail["yahoo_description"].lower()


def test_403_still_raises_when_the_body_is_not_json():
    bad = httpx.Response(
        status_code=403, text="<html>gateway</html>",
        request=httpx.Request("GET", "https://fantasysports.yahooapis.com/x"),
    )
    with pytest.raises(YahooAuthError):
        _raise_for_yahoo_auth(bad, "game/nfl")


# ---------------------------------------------------------------------------
# 401 — the user's grant, which reconnecting DOES fix
# ---------------------------------------------------------------------------

def test_401_is_a_400_telling_the_user_to_reconnect():
    with pytest.raises(YahooAuthError) as exc:
        _raise_for_yahoo_auth(_resp(401, {"error": {"description": "invalid_token"}}),
                              "users;use_login=1/games")
    err = exc.value
    assert err.status_code == 400
    assert err.detail["action"] == "connect"
    assert "reconnect" in err.message.lower()


def test_401_and_403_are_told_apart():
    """The whole point: same exception type, different status and different remedy."""
    with pytest.raises(YahooAuthError) as a:
        _raise_for_yahoo_auth(_resp(401), "p")
    with pytest.raises(YahooAuthError) as b:
        _raise_for_yahoo_auth(_resp(403, APP_403), "p")
    assert a.value.status_code != b.value.status_code
    assert a.value.detail.get("action") == "connect"
    assert "action" not in b.value.detail      # reconnecting cannot fix an app-level 403


# ---------------------------------------------------------------------------
# Everything else is untouched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 404, 429, 500, 503])
def test_non_auth_statuses_pass_straight_through(status):
    _raise_for_yahoo_auth(_resp(status), "p")   # must not raise


@pytest.mark.asyncio
async def test_api_get_maps_a_live_403_instead_of_leaking_httpx(monkeypatch):
    """End-to-end through the helper the routers actually call."""
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _resp(403, APP_403)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    with pytest.raises(YahooAuthError):
        await _api_get_with_token("users;use_login=1/games", "tok")


@pytest.mark.asyncio
async def test_api_get_returns_json_on_success(monkeypatch):
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _resp(200, {"fantasy_content": {"ok": 1}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    assert await _api_get_with_token("p", "tok") == {"fantasy_content": {"ok": 1}}
