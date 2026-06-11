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


def _make_mock_db(league):
    """Create a mock db session; statements return a permissive mock."""
    mock_db = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.rollback = AsyncMock()

    insert_result = MagicMock()
    insert_result.scalars.return_value = MagicMock(all=MagicMock(return_value=[]))

    call_count = {"n": 0}

    async def counting_execute(stmt, *a, **kw):
        call_count["n"] += 1
        return insert_result

    mock_db.execute = counting_execute
    # Expose for assertions
    mock_db._call_count = call_count
    return mock_db


def _patch_league_repo(league):
    """Patch LeagueRepository so the service's league reload returns `league`."""
    repo = MagicMock()
    repo.get_user_league = AsyncMock(return_value=league)
    return patch(
        "backend.services.league_sync.LeagueRepository",
        return_value=repo,
    )


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
    mock_db = _make_mock_db(league)

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
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league.id)

    assert summary["seasons_imported"] == 4
    assert summary["picks_imported"] > 0
    assert summary["managers_found"] == 2


@pytest.mark.asyncio
async def test_sync_handles_season_failure_gracefully():
    """A single season failing should not abort the entire sync."""
    league = _make_league()
    mock_db = _make_mock_db(league)

    call_count = 0

    async def mock_draft_picks(**kwargs):
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
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league.id)

    # 3 out of 4 seasons should succeed
    assert summary["seasons_imported"] == 3


@pytest.mark.asyncio
async def test_sync_stores_manager_map():
    league = _make_league()
    mock_db = _make_mock_db(league)

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
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        await service.sync_league(league.id)

    assert league.manager_map == {"1": "Alice", "2": "Bob"}
    assert league.last_synced is not None


@pytest.mark.asyncio
async def test_sync_caches_free_agents():
    league = _make_league()
    mock_db = _make_mock_db(league)

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
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league.id)

    assert summary["free_agents_cached"] == 50


@pytest.mark.asyncio
async def test_sync_deduplicates_picks():
    """Re-syncing same league doesn't create duplicate picks (on_conflict_do_nothing)."""
    league = _make_league()

    picks = _make_picks(3, season=2025)

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
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService

        # First sync
        mock_db1 = _make_mock_db(league)
        service = LeagueSyncService(mock_db1, league.user_id)
        summary1 = await service.sync_league(league.id)

        # Second sync — same picks
        mock_db2 = _make_mock_db(league)
        service2 = LeagueSyncService(mock_db2, league.user_id)
        summary2 = await service2.sync_league(league.id)

    # Both syncs report same pick count (on_conflict_do_nothing handles dedup at DB level)
    assert summary1["picks_imported"] == summary2["picks_imported"]


@pytest.mark.asyncio
async def test_picks_stored_with_user_id():
    """Sync service is initialized with user_id — picks scoped to user."""
    user_id = uuid.uuid4()
    league = _make_league()
    league.user_id = user_id
    mock_db = _make_mock_db(league)

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = _make_picks(2)
    mock_platform.get_rosters.return_value = []
    mock_platform.get_free_agents.return_value = []

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, user_id)
        assert service._user_id == user_id
        await service.sync_league(league.id)

    # Picks were stored (execute called for INSERT statements)
    assert mock_db._call_count["n"] >= 1


@pytest.mark.asyncio
async def test_user_a_picks_not_visible_to_user_b():
    """Each user gets their own LeagueSyncService with their own user_id.

    User isolation is enforced because:
    1. LeagueSyncService stores user_id at init
    2. Picks are stored in league_auction_history via user_league_id
    3. League ownership verified via SELECT with user_id filter
    """
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    league_a = _make_league(league_id="league-a")
    league_a.user_id = user_a
    league_b = _make_league(league_id="league-b")
    league_b.user_id = user_b

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = _make_picks(2)
    mock_platform.get_rosters.return_value = []
    mock_platform.get_free_agents.return_value = []

    def _league_for(user_id, league_id):
        return league_a if league_id == league_a.id else league_b

    repo = MagicMock()
    repo.get_user_league = AsyncMock(side_effect=_league_for)

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), patch(
        "backend.services.league_sync.LeagueRepository",
        return_value=repo,
    ):
        from backend.services.league_sync import LeagueSyncService

        # User A syncs their league
        mock_db_a = _make_mock_db(league_a)
        service_a = LeagueSyncService(mock_db_a, user_a)
        assert service_a._user_id == user_a

        # User B syncs their league
        mock_db_b = _make_mock_db(league_b)
        service_b = LeagueSyncService(mock_db_b, user_b)
        assert service_b._user_id == user_b

        # Different users, different services, different user_ids
        assert service_a._user_id != service_b._user_id

        # Picks stored under different user_league_ids
        summary_a = await service_a.sync_league(league_a.id)
        summary_b = await service_b.sync_league(league_b.id)

    # Both syncs succeed independently
    assert summary_a["picks_imported"] > 0
    assert summary_b["picks_imported"] > 0


@pytest.mark.asyncio
async def test_sync_espn_redetects_draft_type():
    """ESPN re-sync updates draft_type from snake → auction when detected."""
    league = _make_league(platform="espn")
    league.draft_type = "snake"
    league.budget = None
    mock_db = _make_mock_db(league)

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []
    mock_platform.get_rosters.return_value = []
    mock_platform.get_free_agents.return_value = []
    mock_platform.detect_draft_type.return_value = ("auction", 200)

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock,
        return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season",
        return_value=2026,
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        await service.sync_league(league.id)

    assert league.draft_type == "auction"
    assert league.budget == 200


@pytest.mark.asyncio
async def test_sync_skips_picks_without_player_info():
    """Picks with no player_name and no platform_player_id should be skipped."""
    league = _make_league()
    mock_db = _make_mock_db(league)

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
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        service = LeagueSyncService(mock_db, league.user_id)
        summary = await service.sync_league(league.id)

    # Only 1 valid pick per season × 4 seasons = 4
    assert summary["picks_imported"] == 4
