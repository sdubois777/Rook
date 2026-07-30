"""The Yahoo authorization URL must be a well-formed query string.

It was hand-joined with f"{k}={v}", so redirect_uri's "://" and "/" went out raw and the
state's base64 padding "=" landed unescaped in a query value. Yahoo answers a malformed
authorize request with a generic "Please specify a valid request and submit again" page,
which gives no clue which parameter is at fault.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from backend.integrations import yahoo_api


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(yahoo_api.settings, "yahoo_client_id", "abc.123", raising=False)
    monkeypatch.setattr(
        yahoo_api.settings, "yahoo_redirect_uri",
        "https://localhost:8000/api/auth/yahoo/callback", raising=False)


def _params(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_redirect_uri_round_trips_exactly(cfg):
    """Yahoo compares redirect_uri byte-for-byte against the registered value, so any
    mangling here reads as 'unregistered redirect URI' at the far end."""
    p = _params(yahoo_api.get_authorization_url())
    assert p["redirect_uri"] == "https://localhost:8000/api/auth/yahoo/callback"


def test_the_uri_is_actually_encoded_on_the_wire(cfg):
    url = yahoo_api.get_authorization_url()
    # The raw "https://" must not appear in the query itself.
    assert "redirect_uri=https://" not in url
    assert "redirect_uri=https%3A%2F%2F" in url


def test_required_oauth_params_are_present(cfg):
    p = _params(yahoo_api.get_authorization_url())
    assert p["response_type"] == "code"
    assert p["client_id"] == "abc.123"


def test_state_round_trips_including_base64_padding(cfg):
    """_sign_state emits '<urlsafe_b64>.<hex>', which can carry '=' padding. Unencoded,
    that is a reserved character sitting inside a query value."""
    state = "eyJhIjoiYiJ9==.deadbeef"
    p = _params(yahoo_api.get_authorization_url(state=state))
    assert p["state"] == state


@pytest.mark.parametrize("hostile", [
    "a&b=c",          # would inject a parameter
    "a b",            # space
    "a+b",            # '+' decodes to space unless encoded
    "a/b?c=d#e",
])
def test_a_hostile_state_cannot_break_out_of_its_value(cfg, hostile):
    p = _params(yahoo_api.get_authorization_url(state=hostile))
    assert p["state"] == hostile
    # And it must not have created extra params.
    assert set(p) == {"client_id", "redirect_uri", "response_type", "language", "state"}


def test_no_state_means_no_state_param(cfg):
    assert "state" not in _params(yahoo_api.get_authorization_url())


def test_points_at_yahoos_authorize_endpoint(cfg):
    assert yahoo_api.get_authorization_url().startswith(yahoo_api._YAHOO_AUTH_URL + "?")
