"""Tests for YahooLeagueAPI — token refresh on expiry."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.yahoo_league_api import YahooLeagueAPI


def _make_league():
    league = MagicMock()
    league.league_id = "test-league"
    league.user_id = uuid.uuid4()
    league.season_year = 2026
    league.platform = "yahoo"
    return league


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
