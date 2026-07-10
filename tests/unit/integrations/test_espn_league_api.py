"""Tests for ESPN LeaguePlatformAPI implementation."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.espn_league_api import ESPNLeagueAPI, _ESPN_POS


def _make_league(league_id="12345", season_year=2026):
    league = MagicMock()
    league.league_id = league_id
    league.season_year = season_year
    league.platform = "espn"
    league.user_id = "user-1"
    return league


def test_espn_position_mapping():
    assert _ESPN_POS[1] == "QB"
    assert _ESPN_POS[2] == "RB"
    assert _ESPN_POS[3] == "WR"
    assert _ESPN_POS[4] == "TE"
    assert _ESPN_POS[5] == "K"
    assert _ESPN_POS[16] == "DEF"


def test_init_sets_cookies():
    league = _make_league()
    api = ESPNLeagueAPI(
        league=league, espn_s2="test_s2", swid="{TEST-SWID}"
    )
    assert api._cookies == {"espn_s2": "test_s2", "SWID": "{TEST-SWID}"}


@pytest.mark.asyncio
async def test_validate_cookies_calls_msettings():
    league = _make_league()
    api = ESPNLeagueAPI(
        league=league, espn_s2="s2", swid="{SWID}"
    )
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"settings": {}}
        result = await api.validate_cookies()
        assert result is True
        mock_get.assert_called_once_with("mSettings")


@pytest.mark.asyncio
async def test_get_rosters_parses_teams():
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "teams": [
            {
                "id": 1,
                "name": "Team Alpha",
                "roster": {
                    "entries": [
                        {
                            "playerPoolEntry": {
                                "player": {
                                    "id": 4040715,
                                    "fullName": "Josh Allen",
                                    "defaultPositionId": 1,
                                }
                            }
                        }
                    ]
                },
            }
        ]
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        rosters = await api.get_rosters()
        assert len(rosters) == 1
        assert rosters[0].team_name == "Team Alpha"
        assert rosters[0].players[0].player_name == "Josh Allen"
        assert rosters[0].players[0].position == "QB"


@pytest.mark.asyncio
async def test_get_draft_picks_parses_auction():
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "draftDetail": {
            "picks": [
                {
                    "playerId": 4040715,
                    "teamId": 1,
                    "overallPickNumber": 1,
                    "roundId": 1,
                    "bidAmount": 55,
                },
                {
                    "playerId": 3116406,
                    "teamId": 2,
                    "overallPickNumber": 2,
                    "roundId": 1,
                    "bidAmount": 48,
                },
            ]
        }
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        picks = await api.get_draft_picks()
        assert len(picks) == 2
        assert picks[0].auction_price == 55
        assert picks[0].pick_number == 1
        assert picks[1].picked_by_team_id == "2"


@pytest.mark.asyncio
async def test_detect_draft_type_auction():
    """Picks with bidAmount > 0 → auction."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "draftDetail": {
            "picks": [
                {"playerId": 1, "bidAmount": 55},
                {"playerId": 2, "bidAmount": 0},
            ]
        }
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction"
    assert budget == 200


@pytest.mark.asyncio
async def test_detect_draft_type_snake():
    """All picks with bidAmount=0 or None → snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "draftDetail": {
            "picks": [
                {"playerId": 1, "bidAmount": 0},
                {"playerId": 2},  # no bidAmount key
            ]
        }
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_detect_draft_type_no_picks():
    """Empty picks list → default snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {"draftDetail": {"picks": []}}
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_detect_draft_type_api_error_defaults_snake():
    """API failure → graceful fallback to snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = Exception("API timeout")
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake"
    assert budget is None


def _view_dispatch(views):
    """AsyncMock side_effect returning a per-view response (mSettings / mDraftDetail)."""
    async def _g(view, season=None):
        return views.get(view, {})
    return _g


@pytest.mark.asyncio
async def test_detect_auction_from_draftsettings_reads_real_budget():
    """The fix: draftSettings.type='AUCTION' → auction + the REAL auctionBudget, WITHOUT
    any bidAmount (undrafted). The authoritative flag must win over the empty picks that
    used to mis-store undrafted auction leagues as snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    views = {
        "mSettings": {"settings": {"draftSettings": {"type": "AUCTION", "auctionBudget": 250}}},
        # picks exist pre-draft but all bidAmount 0 — the old path would say snake
        "mDraftDetail": {"draftDetail": {"picks": [{"playerId": 1, "bidAmount": 0}]}},
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction"
    assert budget == 250                              # real budget, NOT hard-coded 200
    mock_get.assert_awaited_once_with("mSettings")     # never consulted mDraftDetail


@pytest.mark.asyncio
async def test_detect_auction_default_budget_when_absent():
    """type='AUCTION' with no auctionBudget → Yahoo-style default 200."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")
    views = {"mSettings": {"settings": {"draftSettings": {"type": "AUCTION"}}}}
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction" and budget == 200


@pytest.mark.asyncio
async def test_detect_snake_from_draftsettings_wins_over_bids():
    """type='SNAKE' → snake + no budget, even if mDraftDetail had bids — the flag is
    authoritative (won't false-auction)."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")
    views = {
        "mSettings": {"settings": {"draftSettings": {"type": "SNAKE"}}},
        "mDraftDetail": {"draftDetail": {"picks": [{"playerId": 1, "bidAmount": 99}]}},
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake" and budget is None


@pytest.mark.asyncio
async def test_detect_falls_back_to_bids_when_type_absent():
    """draftSettings.type absent → fall back to the post-draft bidAmount signal."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")
    views = {
        "mSettings": {"settings": {"draftSettings": {}}},   # no type
        "mDraftDetail": {"draftDetail": {"picks": [{"playerId": 1, "bidAmount": 40}]}},
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction" and budget == 200      # via fallback


@pytest.mark.asyncio
async def test_create_raises_without_cookies():
    league = _make_league()

    with patch(
        "backend.integrations.espn_league_api.CredentialRepository"
    ) as MockRepo:
        mock_repo_instance = AsyncMock()
        mock_repo_instance.get_espn_cookies.return_value = None
        MockRepo.return_value = mock_repo_instance

        from backend.core.exceptions import AppError
        with pytest.raises(AppError, match="ESPN not connected"):
            await ESPNLeagueAPI.create(league, AsyncMock())
