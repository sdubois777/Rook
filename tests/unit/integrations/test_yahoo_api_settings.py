"""Tests for get_league_settings() — Yahoo league settings parsing."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.integrations.yahoo_api import get_league_settings, yahoo_league_key


def _mock_yahoo_response(
    name="Test League",
    num_teams=12,
    draft_type="auction",
    scoring_type="head",
    season="2026",
    is_finished=0,
    stat_mods=None,
    playoff_start_week=15,
    waiver_type="",
):
    """Build a mock Yahoo /league/settings response."""
    if stat_mods is None:
        # Default: PPR (stat_id 11 = 1.0)
        stat_mods = [
            {"stat": {"stat_id": "11", "value": "1.00"}},
            {"stat": {"stat_id": "4", "value": "0.04"}},
        ]

    return {
        "fantasy_content": {
            "league": [
                {
                    "league_key": f"470.l.12345",
                    "league_id": "12345",
                    "name": name,
                    "num_teams": num_teams,
                    "draft_type": draft_type,
                    "scoring_type": scoring_type,
                    "season": season,
                    "is_finished": is_finished,
                },
                {
                    "settings": [
                        {
                            "stat_modifiers": {
                                "stats": stat_mods,
                            },
                            "playoff_start_week": str(playoff_start_week),
                            "waiver_type": waiver_type,
                            "trade_end_date": "2026-11-15",
                        }
                    ]
                },
            ]
        }
    }


@pytest.mark.asyncio
async def test_get_league_settings_parses_name():
    """Name and num_teams are extracted from league metadata."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(
            name="My Fantasy League", num_teams=10
        ),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["name"] == "My Fantasy League"
    assert result["num_teams"] == 10


@pytest.mark.asyncio
async def test_get_league_settings_ppr_from_stat_modifiers():
    """Reception modifier 1.0 → ppr."""
    mods = [{"stat": {"stat_id": "11", "value": "1.00"}}]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(stat_mods=mods),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["scoring_type"] == "ppr"


@pytest.mark.asyncio
async def test_get_league_settings_half_ppr():
    """Reception modifier 0.5 → half_ppr."""
    mods = [{"stat": {"stat_id": "11", "value": "0.50"}}]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(stat_mods=mods),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["scoring_type"] == "half_ppr"


@pytest.mark.asyncio
async def test_get_league_settings_standard_scoring():
    """Reception modifier 0.0 or absent → standard."""
    mods = [{"stat": {"stat_id": "11", "value": "0.00"}}]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(stat_mods=mods),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["scoring_type"] == "standard"


@pytest.mark.asyncio
async def test_get_league_settings_no_reception_stat():
    """Missing reception stat_id → standard."""
    mods = [{"stat": {"stat_id": "4", "value": "0.04"}}]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(stat_mods=mods),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["scoring_type"] == "standard"


@pytest.mark.asyncio
async def test_get_league_settings_auction_draft():
    """draft_type 'auction' → auction."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(draft_type="auction"),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["draft_type"] == "auction"
    assert result["auction_budget"] is not None


@pytest.mark.asyncio
async def test_get_league_settings_snake_draft():
    """draft_type 'live' → snake, no auction budget."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(draft_type="live"),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["draft_type"] == "snake"
    assert result["auction_budget"] is None


@pytest.mark.asyncio
async def test_get_league_settings_playoff_week():
    """Playoff start week parsed correctly."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(playoff_start_week=14),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["playoff_start_week"] == 14


def test_yahoo_league_key_construction():
    """yahoo_league_key builds correct key from league_id + season."""
    assert yahoo_league_key("12345", 2026) == "470.l.12345"
    assert yahoo_league_key("12345", 2024) == "449.l.12345"
    assert yahoo_league_key("99999", 2025) == "461.l.99999"
