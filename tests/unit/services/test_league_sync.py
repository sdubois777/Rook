"""Tests for LeagueSyncService."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.platform_models import (
    DraftPick,
    FreeAgent,
    TeamRoster,
)


def _make_league(platform="yahoo", league_id="test-123"):
    league = MagicMock()
    league.id = uuid.uuid4()
    league.user_id = uuid.uuid4()
    league.platform = platform
    league.league_id = league_id
    league.season_year = 2026
    league.team_count = 12
    league.last_synced = None
    league.manager_map = None
    return league


def _make_picks(count=5, season=2025):
    """Generate dummy draft picks."""
    picks = []
    for i in range(count):
        picks.append(DraftPick(
            platform_player_id=f"player_{i}",
            player_name=f"Player {i}",
            position="WR",
            team_abbr="NYG",
            picked_by_team_id=str(i % 12 + 1),
            manager_name=f"Manager {i % 12}",
            pick_number=i + 1,
            round_number=1,
            auction_price=10 + i,
        ))
    return picks


def _make_rosters(count=3):
    """Generate dummy rosters."""
    return [
        TeamRoster(
            platform_team_id=str(i + 1),
            manager_name=f"Manager {i}",
            team_name=f"Team {i}",
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_sync_imports_up_to_4_seasons():
    league = _make_league()
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = _make_picks(3)
    mock_platform.get_rosters.return_value = _make_rosters(2)
    mock_platform.get_free_agents.return_value = []

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), patch(
        "backend.services.league_sync.LeagueRepository",
    ):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league)

    assert summary["seasons_imported"] == 4
    assert summary["picks_imported"] > 0
    assert summary["managers_found"] == 2


@pytest.mark.asyncio
async def test_sync_handles_season_failure_gracefully():
    """A single season failing should not abort the entire sync."""
    league = _make_league()
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    call_count = 0

    async def mock_draft_picks():
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("ESPN API timeout for this season")
        return _make_picks(2)

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.side_effect = mock_draft_picks
    mock_platform.get_rosters.return_value = []
    mock_platform.get_free_agents.return_value = []

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), patch(
        "backend.services.league_sync.LeagueRepository",
    ):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league)

    # 3 out of 4 seasons should succeed
    assert summary["seasons_imported"] == 3


@pytest.mark.asyncio
async def test_sync_stores_manager_map():
    league = _make_league()
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    rosters = [
        TeamRoster(
            platform_team_id="1",
            manager_name="Alice",
            team_name="Team A",
        ),
        TeamRoster(
            platform_team_id="2",
            manager_name="Bob",
            team_name="Team B",
        ),
    ]

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []
    mock_platform.get_rosters.return_value = rosters
    mock_platform.get_free_agents.return_value = []

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), patch(
        "backend.services.league_sync.LeagueRepository",
    ):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        await service.sync_league(league)

    assert league.manager_map == {"1": "Alice", "2": "Bob"}
    assert league.last_synced is not None


@pytest.mark.asyncio
async def test_sync_caches_free_agents():
    league = _make_league()
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    free_agents = [
        FreeAgent(
            platform_player_id=str(i),
            player_name=f"FA {i}",
            position="WR",
            team_abbr="NE",
        )
        for i in range(50)
    ]

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []
    mock_platform.get_rosters.return_value = []
    mock_platform.get_free_agents.return_value = free_agents

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), patch(
        "backend.services.league_sync.LeagueRepository",
    ):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league)

    assert summary["free_agents_cached"] == 50


@pytest.mark.asyncio
async def test_sync_skips_picks_without_player_info():
    """Picks with no player_name and no platform_player_id should be skipped."""
    league = _make_league()
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    picks = [
        DraftPick(
            platform_player_id="",
            player_name="",
            position="",
            team_abbr="",
            picked_by_team_id="1",
            manager_name="",
            pick_number=1,
            round_number=1,
        ),
        DraftPick(
            platform_player_id="valid_id",
            player_name="Real Player",
            position="RB",
            team_abbr="SF",
            picked_by_team_id="2",
            manager_name="Bob",
            pick_number=2,
            round_number=1,
            auction_price=30,
        ),
    ]

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = picks
    mock_platform.get_rosters.return_value = []
    mock_platform.get_free_agents.return_value = []

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), patch(
        "backend.services.league_sync.LeagueRepository",
    ):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league)

    # Only 1 valid pick per season × 4 seasons = 4
    assert summary["picks_imported"] == 4
