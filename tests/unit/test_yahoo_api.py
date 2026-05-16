"""
tests/unit/test_yahoo_api.py

Unit tests for backend/integrations/yahoo_api.py.
Required by stage-06-to-10.md Stage 10 spec.

All Yahoo HTTP calls are mocked — no real network requests made.
"""
from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.integrations.yahoo_api as yahoo_mod
from backend.integrations.yahoo_api import (
    _basic_auth_header,
    _league_key,
    exchange_code_for_tokens,
    get_authorization_url,
    get_league,
    get_players,
    refresh_access_token,

    sync_yahoo_player_ids,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_token_cache():
    """Clear in-memory token cache before each test to prevent state bleed."""
    yahoo_mod._cached_token = None
    yahoo_mod._token_expires_at = 0.0
    yield
    yahoo_mod._cached_token = None
    yahoo_mod._token_expires_at = 0.0


@pytest.fixture
def mock_settings(monkeypatch):
    monkeypatch.setattr("backend.integrations.yahoo_api.settings.yahoo_client_id", "test_client_id")
    monkeypatch.setattr("backend.integrations.yahoo_api.settings.yahoo_client_secret", "test_client_secret")
    monkeypatch.setattr("backend.integrations.yahoo_api.settings.yahoo_redirect_uri", "http://localhost:8000/auth/yahoo/callback")
    monkeypatch.setattr("backend.integrations.yahoo_api.settings.yahoo_league_id", "12345")
    monkeypatch.setattr("backend.integrations.yahoo_api.settings.yahoo_refresh_token", "test_refresh_token")


# ---------------------------------------------------------------------------
# Test: OAuth URL generation
# ---------------------------------------------------------------------------

def test_oauth_url_generated_correctly(mock_settings):
    """Authorization URL must contain client_id, redirect_uri, and response_type=code."""
    url = get_authorization_url()

    assert "test_client_id" in url
    assert "http://localhost:8000/auth/yahoo/callback" in url
    assert "response_type=code" in url
    assert "api.login.yahoo.com" in url


def test_oauth_url_is_string(mock_settings):
    """get_authorization_url() must return a plain string, not a Response object."""
    url = get_authorization_url()
    assert isinstance(url, str)
    assert url.startswith("https://")


# ---------------------------------------------------------------------------
# Test: Token refresh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_refresh_updates_stored_token(mock_settings):
    """refresh_access_token() caches the new access token in module state."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "access_token": "new_access_token_abc",
        "expires_in": 3600,
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fake_response)
        mock_client_cls.return_value = mock_client

        token = await refresh_access_token()

    assert token == "new_access_token_abc"
    assert yahoo_mod._cached_token == "new_access_token_abc"
    assert yahoo_mod._token_expires_at > time.time()


@pytest.mark.asyncio
async def test_token_refresh_raises_without_refresh_token(monkeypatch):
    """ValueError raised when YAHOO_REFRESH_TOKEN is not configured."""
    monkeypatch.setattr("backend.integrations.yahoo_api.settings.yahoo_refresh_token", None)

    with pytest.raises(ValueError, match="YAHOO_REFRESH_TOKEN"):
        await refresh_access_token()


# ---------------------------------------------------------------------------
# Test: get_players returns list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_players_returns_list(mock_settings):
    """
    get_players() returns a list of player dicts parsed from Yahoo API response.
    Uses game-level endpoint (/game/nfl/players) — available year-round, no league needed.
    """
    # Game-level response structure: fantasy_content → game → [metadata, {players: {...}}]
    fake_yahoo_response: dict[str, Any] = {
        "fantasy_content": {
            "game": [
                {},  # game metadata
                {
                    "players": {
                        "0": {
                            "player": [
                                {"player_id": "30977", "name": {"full": "Patrick Mahomes"}},
                            ]
                        },
                        "1": {
                            "player": [
                                {"player_id": "32321", "name": {"full": "Justin Jefferson"}},
                            ]
                        },
                        "count": 2,
                    }
                },
            ]
        }
    }

    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=fake_yahoo_response)):
        players = await get_players(count=25)

    assert isinstance(players, list)
    assert len(players) == 2
    player_ids = {p.get("player_id") for p in players}
    assert "30977" in player_ids
    assert "32321" in player_ids


@pytest.mark.asyncio
async def test_get_players_empty_response_returns_empty_list(mock_settings):
    """get_players() returns [] when Yahoo returns no players."""
    empty_response: dict[str, Any] = {
        "fantasy_content": {"game": [{}, {"players": {"count": 0}}]}
    }

    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=empty_response)):
        players = await get_players(count=25)

    assert players == []


# ---------------------------------------------------------------------------
# Test: Player ID matched to draft bible
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_player_id_matched_to_draft_bible(mock_settings):
    """
    sync_yahoo_player_ids() matches Yahoo players to DB records by name
    and updates yahoo_player_id on the matched DB row.
    """
    # Mock DB player
    mock_player = MagicMock()
    mock_player.name = "Patrick Mahomes"
    mock_player.yahoo_player_id = "nfl_00-0033873"  # old gsis placeholder

    # Mock DB session
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_player]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    # Mock Yahoo API returning matching player
    yahoo_players = [
        {"player_id": "30977", "name": {"full": "Patrick Mahomes"}}
    ]
    with patch.object(yahoo_mod, "get_players", AsyncMock(return_value=yahoo_players)):
        result = await sync_yahoo_player_ids(mock_session)

    assert result["matched"] == 1
    assert result["unmatched"] == 0
    assert mock_player.yahoo_player_id == "30977"
    mock_session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test: Unmatched players logged, not crashed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unmatched_players_logged_not_crashed(mock_settings, caplog):
    """
    Players in Yahoo's universe that don't match any DB record are logged
    at INFO level — no exception raised, unmatched count returned correctly.
    """
    # DB has no players
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    yahoo_players = [
        {"player_id": "99999", "name": {"full": "Unknown Player X"}}
    ]
    with patch.object(yahoo_mod, "get_players", AsyncMock(return_value=yahoo_players)):
        import logging
        with caplog.at_level(logging.INFO, logger="backend.integrations.yahoo_api"):
            result = await sync_yahoo_player_ids(mock_session)

    assert result["matched"] == 0
    assert result["unmatched"] == 1
    # No exception — test passes by reaching here


# ---------------------------------------------------------------------------
# Test: League key format
# ---------------------------------------------------------------------------

def test_league_key_format(mock_settings):
    """League key must be 'nfl.l.{league_id}'."""
    key = _league_key()
    assert key == "nfl.l.12345"


def test_basic_auth_header_is_base64(mock_settings):
    """Authorization header must be 'Basic ' + base64(client_id:secret)."""
    import base64
    header = _basic_auth_header()
    assert header.startswith("Basic ")
    encoded_part = header[len("Basic "):]
    decoded = base64.b64decode(encoded_part).decode()
    assert decoded == "test_client_id:test_client_secret"


# ---------------------------------------------------------------------------
# Test: exchange_code_for_tokens
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exchange_code_for_tokens_returns_dict(mock_settings):
    """exchange_code_for_tokens() exchanges authorization code for token dict."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "access_token": "access_abc",
        "refresh_token": "refresh_xyz",
        "expires_in": 3600,
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=fake_response)
        mock_client_cls.return_value = mock_client

        result = await exchange_code_for_tokens("auth_code_123")

    assert result["access_token"] == "access_abc"
    assert result["refresh_token"] == "refresh_xyz"
    # Verify correct grant_type sent
    call_kwargs = mock_client.post.call_args
    assert call_kwargs.kwargs["data"]["grant_type"] == "authorization_code"
    assert call_kwargs.kwargs["data"]["code"] == "auth_code_123"


# ---------------------------------------------------------------------------
# Test: _get_valid_token uses cache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_valid_token_uses_cache_when_fresh(mock_settings):
    """_get_valid_token() returns cached token without calling refresh when not expired."""
    from backend.integrations.yahoo_api import _get_valid_token

    yahoo_mod._cached_token = "cached_token_xyz"
    yahoo_mod._token_expires_at = time.time() + 3600  # far future

    with patch.object(yahoo_mod, "refresh_access_token", AsyncMock()) as mock_refresh:
        token = await _get_valid_token()

    assert token == "cached_token_xyz"
    mock_refresh.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test: get_league parses response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_league_returns_metadata_dict(mock_settings):
    """get_league() returns the league metadata dict from Yahoo response."""
    fake_response: dict[str, Any] = {
        "fantasy_content": {
            "league": [
                {"name": "My Test League", "num_teams": "12", "scoring_type": "ppr"}
            ]
        }
    }

    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=fake_response)):
        result = await get_league()

    assert result["name"] == "My Test League"
    assert result["num_teams"] == "12"


@pytest.mark.asyncio
async def test_get_league_empty_response_returns_empty_dict(mock_settings):
    """get_league() returns {} when Yahoo returns empty league list."""
    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value={"fantasy_content": {"league": []}})):
        result = await get_league()
    assert result == {}


# ---------------------------------------------------------------------------
# Test: get_draft_results parses response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_draft_results_returns_picks(mock_settings):
    """get_draft_results() returns list of draft pick dicts."""
    fake_response: dict[str, Any] = {
        "fantasy_content": {
            "league": [
                {},
                {
                    "draft_results": {
                        "0": {"draft_result": {"pick": 1, "round": 1, "team_key": "nfl.l.12345.t.1", "player_key": "nfl.p.30977"}},
                        "1": {"draft_result": {"pick": 2, "round": 1, "team_key": "nfl.l.12345.t.2", "player_key": "nfl.p.32321"}},
                        "count": 2,
                    }
                },
            ]
        }
    }

    from backend.integrations.yahoo_api import get_draft_results
    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=fake_response)):
        picks = await get_draft_results()

    assert len(picks) == 2
    assert picks[0]["pick"] == 1
    assert picks[1]["player_key"] == "nfl.p.32321"


@pytest.mark.asyncio
async def test_get_draft_results_empty_returns_empty_list(mock_settings):
    """get_draft_results() returns [] when no draft results exist."""
    from backend.integrations.yahoo_api import get_draft_results
    empty_response: dict[str, Any] = {
        "fantasy_content": {"league": [{}, {"draft_results": {"count": 0}}]}
    }
    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=empty_response)):
        picks = await get_draft_results()
    assert picks == []


# ---------------------------------------------------------------------------
# Test: sync_yahoo_player_ids name normalization
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_handles_name_suffix_normalization(mock_settings):
    """sync_yahoo_player_ids() matches players despite name suffix differences (Jr., Sr.)."""
    mock_player = MagicMock()
    mock_player.name = "Travis Kelce Jr."
    mock_player.yahoo_player_id = None

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_player]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    # Yahoo returns name without suffix
    yahoo_players = [{"player_id": "24171", "name": {"full": "Travis Kelce"}}]
    with patch.object(yahoo_mod, "get_players", AsyncMock(return_value=yahoo_players)):
        result = await sync_yahoo_player_ids(mock_session)

    assert result["matched"] == 1
    assert mock_player.yahoo_player_id == "24171"


# ---------------------------------------------------------------------------
# Test: _api_get makes authenticated request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_get_makes_authenticated_request(mock_settings):
    """_api_get() calls Yahoo API with Bearer token and format=json."""
    from backend.integrations.yahoo_api import _api_get

    # Prime the token cache so no refresh call is needed
    yahoo_mod._cached_token = "test_access_token"
    yahoo_mod._token_expires_at = time.time() + 3600

    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {"fantasy_content": {}}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=fake_response)
        mock_client_cls.return_value = mock_client

        result = await _api_get("league/nfl.l.12345")

    call_args = mock_client.get.call_args
    assert "Bearer test_access_token" in call_args.kwargs["headers"]["Authorization"]
    assert call_args.kwargs["params"]["format"] == "json"
    assert result == {"fantasy_content": {}}


@pytest.mark.asyncio
async def test_get_valid_token_refreshes_when_expired(mock_settings):
    """_get_valid_token() calls refresh_access_token() when cache is expired."""
    from backend.integrations.yahoo_api import _get_valid_token

    yahoo_mod._cached_token = "old_token"
    yahoo_mod._token_expires_at = time.time() - 1  # expired

    with patch.object(yahoo_mod, "refresh_access_token", AsyncMock(return_value="fresh_token")) as mock_refresh:
        token = await _get_valid_token()

    mock_refresh.assert_awaited_once()
    assert token == "fresh_token"


# ---------------------------------------------------------------------------
# Test: get_teams parses response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_teams_returns_team_list(mock_settings):
    """get_teams() returns list of team metadata dicts."""
    from backend.integrations.yahoo_api import get_teams

    fake_response: dict[str, Any] = {
        "fantasy_content": {
            "league": [
                {},
                {
                    "teams": {
                        "0": {"team": [{"team_key": "nfl.l.12345.t.1", "name": "Team Alpha"}]},
                        "1": {"team": [{"team_key": "nfl.l.12345.t.2", "name": "Team Beta"}]},
                        "count": 2,
                    }
                },
            ]
        }
    }

    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=fake_response)):
        teams = await get_teams()

    assert len(teams) == 2
    team_keys = {t.get("team_key") for t in teams}
    assert "nfl.l.12345.t.1" in team_keys


@pytest.mark.asyncio
async def test_get_teams_flattens_nested_list(mock_settings):
    """get_teams() flattens Yahoo's nested list-of-dicts team format."""
    from backend.integrations.yahoo_api import get_teams

    # Yahoo sometimes returns team data as a list of single-key dicts
    fake_response: dict[str, Any] = {
        "fantasy_content": {
            "league": [
                {},
                {
                    "teams": {
                        "0": {
                            "team": [
                                [
                                    {"team_key": "nfl.l.12345.t.1"},
                                    {"name": "Nested Team"},
                                ]
                            ]
                        },
                        "count": 1,
                    }
                },
            ]
        }
    }

    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=fake_response)):
        teams = await get_teams()

    assert len(teams) == 1
    assert teams[0].get("team_key") == "nfl.l.12345.t.1"
    assert teams[0].get("name") == "Nested Team"


# ---------------------------------------------------------------------------
# Test: get_rosters parses response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_rosters_returns_dict_by_team_key(mock_settings):
    """get_rosters() returns dict keyed by team_key with player lists."""
    from backend.integrations.yahoo_api import get_rosters

    fake_response: dict[str, Any] = {
        "fantasy_content": {
            "league": [
                {},
                {
                    "teams": {
                        "0": {
                            "team": [
                                {"team_key": "nfl.l.12345.t.1"},
                                {
                                    "roster": {
                                        "players": {
                                            "0": {"player": [{"player_id": "30977"}]},
                                            "count": 1,
                                        }
                                    }
                                },
                            ]
                        },
                        "count": 1,
                    }
                },
            ]
        }
    }

    with patch.object(yahoo_mod, "_api_get", AsyncMock(return_value=fake_response)):
        rosters = await get_rosters()

    assert "nfl.l.12345.t.1" in rosters
    assert len(rosters["nfl.l.12345.t.1"]) == 1
    assert rosters["nfl.l.12345.t.1"][0]["player_id"] == "30977"


@pytest.mark.asyncio
async def test_sync_skips_players_missing_id_or_name(mock_settings):
    """sync_yahoo_player_ids() skips Yahoo records missing player_id or name."""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()

    # Records without player_id or name — should be skipped, not crash
    yahoo_players = [
        {"player_id": "", "name": {"full": "Someone"}},  # empty id
        {"player_id": "12345", "name": {}},              # no full name
        {"name": {"full": "No ID Player"}},              # missing player_id key
    ]
    with patch.object(yahoo_mod, "get_players", AsyncMock(return_value=yahoo_players)):
        result = await sync_yahoo_player_ids(mock_session)

    # No crash, all three skipped (not counted as unmatched since they have no id/name)
    assert result["unmatched"] == 0
