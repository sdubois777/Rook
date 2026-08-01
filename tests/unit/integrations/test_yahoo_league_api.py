"""Tests for YahooLeagueAPI — token refresh and draft-history handling."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.integrations.yahoo_league_api import YahooLeagueAPI


def _make_league():
    league = MagicMock()
    league.league_id = "test-league"
    league.user_id = uuid.uuid4()
    league.season_year = 2026
    league.platform = "yahoo"
    return league


def _make_api():
    return YahooLeagueAPI(
        league=_make_league(),
        access_token="access",
        refresh_token="refresh",
        expires_at=None,
        credential_repo=AsyncMock(),
        user_id=uuid.uuid4(),
    )


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://fantasysports.yahooapis.com/x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"{status_code} error", request=request, response=response
    )


@pytest.mark.asyncio
async def test_token_refresh_on_expiry():
    """YahooLeagueAPI refreshes access token when expired."""
    league = _make_league()
    repo = AsyncMock()

    # Token expired 10 minutes ago
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    api = YahooLeagueAPI(
        league=league,
        access_token="old_access",
        refresh_token="test_refresh",
        expires_at=expired_at,
        credential_repo=repo,
        user_id=league.user_id,
    )

    # Mock the HTTP call for token refresh
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "expires_in": 3600,
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp

    with patch("httpx.AsyncClient") as MockAsyncClient:
        MockAsyncClient.return_value.__aenter__ = AsyncMock(
            return_value=mock_client
        )
        MockAsyncClient.return_value.__aexit__ = AsyncMock(
            return_value=False
        )

        token = await api._get_token()

    # Should have refreshed and returned new token
    assert token == "new_access"
    assert api._access_token == "new_access"
    assert api._refresh_token == "new_refresh"

    # Repo should store new encrypted tokens
    repo.upsert_yahoo.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_404_on_draft_history_returns_empty():
    """Yahoo 404 on draftresults means no history yet — not an error."""
    api = _make_api()
    with patch.object(api, "_api_get", AsyncMock(side_effect=_http_status_error(404))):
        picks = await api.get_draft_picks(league_key="470.l.46511")
    assert picks == []


@pytest.mark.asyncio
async def test_sync_403_on_draft_history_returns_empty():
    """Yahoo 403 on draftresults (league absent that season) returns empty."""
    api = _make_api()
    with patch.object(api, "_api_get", AsyncMock(side_effect=_http_status_error(403))):
        picks = await api.get_draft_picks(league_key="461.l.46511")
    assert picks == []


@pytest.mark.asyncio
async def test_draft_history_unexpected_error_reraises():
    """Server errors are real failures and must propagate."""
    api = _make_api()
    with patch.object(api, "_api_get", AsyncMock(side_effect=_http_status_error(500))):
        with pytest.raises(httpx.HTTPStatusError):
            await api.get_draft_picks(league_key="470.l.46511")


# ---------------------------------------------------------------------------
# Yahoo says "unknown", never a guess.
#
# Nothing about Yahoo's response shape could be checked against a real response: its
# API refuses unauthenticated requests and no recorded sample exists in this repo. So
# every Yahoo field here reports unknown, and these tests exist to stop someone
# filling them in from documentation alone later.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_matchups_reports_unknown_not_an_empty_week():
    """None means "we could not tell", which makes the caller withhold an opponent.
    An empty list would claim this league genuinely has no game this week — the
    signal the matchup page uses to decide whether to show one."""
    api = _make_api()
    assert await api.get_matchups(week=1) is None


@pytest.mark.asyncio
async def test_roster_players_carry_no_guessed_slot_or_injury():
    """Both fields arrive as None, meaning unknown.

    A wrong lineup slot renders as a confident, valid-looking starting position, and
    a missed injury silently seats a player who cannot play. Neither field name has
    been verified against a real Yahoo response, so neither is populated."""
    api = _make_api()
    roster_payload = {
        "fantasy_content": {"league": [{}, {"teams": {
            "count": 1,
            "0": {"team": [
                [{"team_key": "470.l.1.t.1"}, {"name": "My Team"}],
                {"roster": {"players": {
                    "count": 1,
                    "0": {"player": [[
                        {"player_key": "470.p.100"},
                        {"name": {"full": "Some Player"}},
                        {"display_position": "RB"},
                        {"editorial_team_abbr": "SF"},
                        # Present in the payload and deliberately NOT read:
                        {"status": "Q"},
                        {"selected_position": [{"position": "RB"}]},
                    ]]},
                }}},
            ]},
        }}]}
    }
    with patch.object(api, "_api_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = roster_payload
        rosters = await api.get_rosters()

    player = rosters[0].players[0]
    assert player.player_name == "Some Player"      # the verified fields still parse
    assert player.lineup_slot is None               # unknown, NOT "RB"
    assert player.injury_status is None             # unknown, NOT "Q"
