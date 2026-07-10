"""Tests for Sleeper LeaguePlatformAPI implementation."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.sleeper_league_api import SleeperLeagueAPI


def _make_league(league_id="12345"):
    league = MagicMock()
    league.league_id = league_id
    league.platform = "sleeper"
    return league


@pytest.mark.asyncio
async def test_get_rosters_maps_user_display_name():
    league = _make_league()
    api = SleeperLeagueAPI(league)

    rosters_data = [
        {
            "roster_id": 1,
            "owner_id": "user_abc",
            "players": ["4040715", "3116406"],
            "settings": {"wins": 7, "losses": 3, "waiver_budget_used": 50},
        }
    ]
    users_data = [
        {
            "user_id": "user_abc",
            "display_name": "AlphaManager",
            "metadata": {"team_name": "Alpha Squad"},
        }
    ]

    call_count = 0

    async def mock_get(path):
        nonlocal call_count
        call_count += 1
        if "rosters" in path:
            return rosters_data
        if "users" in path:
            return users_data
        return []

    with patch.object(api, "_get", side_effect=mock_get):
        rosters = await api.get_rosters()
        assert len(rosters) == 1
        assert rosters[0].manager_name == "AlphaManager"
        assert rosters[0].team_name == "Alpha Squad"
        assert len(rosters[0].players) == 2
        assert rosters[0].wins == 7
        assert rosters[0].losses == 3


@pytest.mark.asyncio
async def test_get_draft_picks_with_auction_price():
    league = _make_league()
    api = SleeperLeagueAPI(league)

    drafts_data = [{"draft_id": "draft_001"}]
    picks_data = [
        {
            "player_id": "4040715",
            "roster_id": 1,
            "pick_no": 1,
            "round": 1,
            "amount": 55,
            "metadata": {
                "first_name": "Josh",
                "last_name": "Allen",
                "position": "QB",
                "team": "BUF",
            },
        },
        {
            "player_id": "3116406",
            "roster_id": 2,
            "pick_no": 2,
            "round": 1,
            "amount": None,
            "metadata": {
                "first_name": "Christian",
                "last_name": "McCaffrey",
                "position": "RB",
                "team": "SF",
            },
        },
    ]

    async def mock_get(path):
        if "drafts" in path:
            return drafts_data
        if "picks" in path:
            return picks_data
        return []

    with patch.object(api, "_get", side_effect=mock_get):
        picks = await api.get_draft_picks()
        assert len(picks) == 2
        assert picks[0].player_name == "Josh Allen"
        assert picks[0].auction_price == 55
        assert picks[0].position == "QB"
        assert picks[1].player_name == "Christian McCaffrey"
        assert picks[1].auction_price is None


@pytest.mark.asyncio
async def test_get_free_agents_returns_empty():
    league = _make_league()
    api = SleeperLeagueAPI(league)
    result = await api.get_free_agents()
    assert result == []


@pytest.mark.asyncio
async def test_get_matchups_returns_empty():
    league = _make_league()
    api = SleeperLeagueAPI(league)
    result = await api.get_matchups(week=1)
    assert result == []


@pytest.mark.asyncio
async def test_get_standings_delegates_to_get_rosters():
    league = _make_league()
    api = SleeperLeagueAPI(league)

    with patch.object(
        api, "get_rosters", new_callable=AsyncMock
    ) as mock:
        mock.return_value = ["fake_roster"]
        result = await api.get_standings()
        assert result == ["fake_roster"]
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_roster_missing_owner():
    """Roster with no matching user should still work."""
    league = _make_league()
    api = SleeperLeagueAPI(league)

    rosters_data = [
        {
            "roster_id": 1,
            "owner_id": "unknown_user",
            "players": [],
            "settings": {},
        }
    ]
    users_data = []  # No users returned

    async def mock_get(path):
        if "rosters" in path:
            return rosters_data
        if "users" in path:
            return users_data
        return []

    with patch.object(api, "_get", side_effect=mock_get):
        rosters = await api.get_rosters()
        assert len(rosters) == 1
        assert rosters[0].manager_name == ""


@pytest.mark.asyncio
async def test_get_rosters_captures_owner_and_co_owners():
    """owner_id + co_owners → owner_ids for exact is_me binding (co-owned team)."""
    league = _make_league()
    api = SleeperLeagueAPI(league)
    rosters_data = [
        {"roster_id": 8, "owner_id": "me-777", "co_owners": ["partner-9"], "players": []},
    ]
    users_data = [{"user_id": "me-777", "display_name": "Me", "metadata": {}}]

    async def mock_get(path):
        return rosters_data if "rosters" in path else users_data

    with patch.object(api, "_get", new=mock_get):
        rosters = await api.get_rosters()
    assert rosters[0].platform_team_id == "8"
    assert rosters[0].owner_ids == ["me-777", "partner-9"]   # owner + co-owner
