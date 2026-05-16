"""Tests for get_league_settings() and _detect_draft_type() — Yahoo league settings parsing."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.integrations.yahoo_api import (
    _detect_draft_type,
    get_league_settings,
    yahoo_league_key,
)


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
                    "league_key": "470.l.12345",
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


def _mock_draft_results(picks=None):
    """Build a mock Yahoo /league/draftresults response."""
    if picks is None:
        picks = []
    results = {"count": len(picks)}
    for i, pick in enumerate(picks):
        results[str(i)] = {"draft_result": pick}
    return {
        "fantasy_content": {
            "league": [
                {"league_key": "470.l.12345"},
                {"draft_results": results},
            ]
        }
    }


# ---------------------------------------------------------------------------
# get_league_settings() — scoring and metadata tests
# Mock _detect_draft_type so these tests focus on scoring/name parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_league_settings_parses_name():
    """Name and num_teams are extracted from league metadata."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(
            name="My Fantasy League", num_teams=10
        ),
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("snake", None),
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
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("snake", None),
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
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("snake", None),
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
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("snake", None),
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
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("snake", None),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["scoring_type"] == "standard"


@pytest.mark.asyncio
async def test_get_league_settings_playoff_week():
    """Playoff start week parsed correctly."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(playoff_start_week=14),
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("snake", None),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["playoff_start_week"] == 14


@pytest.mark.asyncio
async def test_get_league_settings_returns_ppr_not_head():
    """scoring_type from stat modifiers is 'ppr', not Yahoo's raw 'head'."""
    mods = [{"stat": {"stat_id": "11", "value": "1.00"}}]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(
            scoring_type="head", stat_mods=mods
        ),
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("auction", 200),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["scoring_type"] == "ppr"
    assert result["scoring_type"] != "head"


# ---------------------------------------------------------------------------
# _detect_draft_type() — auction vs snake from draft pick costs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auction_detected_from_pick_costs():
    """Picks with cost > 0 → auction."""
    picks = [
        {"pick": "1", "round": "1", "cost": "45", "player_key": "470.p.1"},
        {"pick": "2", "round": "1", "cost": "30", "player_key": "470.p.2"},
    ]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_draft_results(picks),
    ):
        draft_type, budget = await _detect_draft_type("tok", "470.l.12345")

    assert draft_type == "auction"
    assert budget == 200


@pytest.mark.asyncio
async def test_snake_detected_from_no_costs():
    """Picks without cost data → snake."""
    picks = [
        {"pick": "1", "round": "1", "player_key": "470.p.1"},
        {"pick": "2", "round": "1", "player_key": "470.p.2"},
    ]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_draft_results(picks),
    ):
        draft_type, budget = await _detect_draft_type("tok", "470.l.12345")

    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_snake_detected_from_zero_costs():
    """Picks with cost=0 → snake (not auction)."""
    picks = [
        {"pick": "1", "round": "1", "cost": "0", "player_key": "470.p.1"},
    ]
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_draft_results(picks),
    ):
        draft_type, budget = await _detect_draft_type("tok", "470.l.12345")

    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_snake_detected_from_empty_draft():
    """No draft results → snake (e.g., league hasn't drafted yet)."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_draft_results([]),
    ):
        draft_type, budget = await _detect_draft_type("tok", "470.l.12345")

    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_detect_draft_type_fallback_on_error():
    """API failure → defaults to snake."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        side_effect=Exception("Yahoo API down"),
    ):
        draft_type, budget = await _detect_draft_type("tok", "470.l.12345")

    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_get_league_settings_uses_detect_for_draft_type():
    """get_league_settings() returns detected auction type, not raw metadata."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(draft_type="live"),
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("auction", 200),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    # Even though raw draft_type is "live", detection found auction
    assert result["draft_type"] == "auction"
    assert result["auction_budget"] == 200


@pytest.mark.asyncio
async def test_get_league_settings_snake_from_detection():
    """get_league_settings() returns snake with None budget when detected."""
    with patch(
        "backend.integrations.yahoo_api._api_get_with_token",
        new_callable=AsyncMock,
        return_value=_mock_yahoo_response(draft_type="live"),
    ), patch(
        "backend.integrations.yahoo_api._detect_draft_type",
        new_callable=AsyncMock,
        return_value=("snake", None),
    ):
        result = await get_league_settings("tok", "470.l.12345")

    assert result["draft_type"] == "snake"
    assert result["auction_budget"] is None


# ---------------------------------------------------------------------------
# yahoo_league_key() — league key construction
# ---------------------------------------------------------------------------


def test_yahoo_league_key_construction():
    """yahoo_league_key builds correct key from league_id + season."""
    assert yahoo_league_key("12345", 2026) == "470.l.12345"
    assert yahoo_league_key("12345", 2024) == "449.l.12345"
    assert yahoo_league_key("99999", 2025) == "461.l.99999"
