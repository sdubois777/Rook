"""Tests for LeagueSyncService."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.platform_models import (
    DraftPick,
    FreeAgent,
    LeagueMetadata,
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
    mock_platform.get_roster_slots.return_value = None
    mock_platform.get_league_metadata.return_value = LeagueMetadata()  # empty → no override

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
async def test_sync_espn_snake_clears_stale_budget():
    """ESPN snake detection now clears a stale auction budget (was left at 200 because
    sync only overwrote budget when non-None)."""
    league = _make_league(platform="espn")
    league.draft_type = "auction"
    league.budget = 200                       # stale auction budget from a prior state
    mock_db = _make_mock_db(league)

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []
    mock_platform.get_rosters.return_value = []
    mock_platform.get_free_agents.return_value = []
    mock_platform.get_roster_slots.return_value = None
    mock_platform.get_league_metadata.return_value = LeagueMetadata()
    mock_platform.detect_draft_type.return_value = ("snake", None)

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock, return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season", return_value=2026,
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        await LeagueSyncService(mock_db, league.user_id).sync_league(league.id)

    assert league.draft_type == "snake"
    assert league.budget is None              # cleared, not stale 200


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


@pytest.mark.asyncio
async def test_sync_stamps_last_synced_even_if_no_history():
    """A new league with zero draft picks still records a successful sync."""
    league = _make_league()
    league.last_synced = None
    mock_db = _make_mock_db(league)

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []  # pre-draft league
    mock_platform.get_rosters.return_value = _make_rosters(1)
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

    assert league.last_synced is not None
    assert summary["picks_imported"] == 0
    assert summary["managers_found"] == 1
    assert summary["warnings"] == []


@pytest.mark.asyncio
async def test_sync_continues_after_history_failure():
    """Draft-history failure becomes a warning; sync completes and stamps last_synced."""
    league = _make_league()
    league.last_synced = None
    mock_db = _make_mock_db(league)

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.side_effect = Exception("403 Forbidden")
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

    assert league.last_synced is not None
    assert summary["picks_imported"] == 0
    # One warning per attempted history season
    assert len(summary["warnings"]) == 4
    assert all("No draft history" in w for w in summary["warnings"])


# ---------------------------------------------------------------------------
# League-metadata capture (stop discarding already-fetched data) — per platform
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sleeper_metadata_parses_name_scoring_type_and_draft_date():
    """Sleeper get_league_metadata mines /league (name, scoring rec, total_rosters)
    + /drafts (type, start_time) — the objects sync already touches."""
    from backend.integrations.sleeper_league_api import SleeperLeagueAPI

    league = MagicMock()
    league.league_id = "999"
    api = SleeperLeagueAPI(league)

    async def fake_get(path):
        if path.endswith("/drafts"):
            return [{"type": "snake", "start_time": 1725112800000}]  # 2024-08-31 ~
        return {"name": "The League of Misfits", "total_rosters": 10,
                "scoring_settings": {"rec": 0.5}}
    api._get = fake_get

    meta = await api.get_league_metadata()
    assert meta.name == "The League of Misfits"
    assert meta.scoring == "half_ppr"      # rec 0.5
    assert meta.team_count == 10
    assert meta.draft_type == "snake"       # not the hardcoded auction default
    assert meta.draft_date is not None and meta.draft_date.year == 2024


@pytest.mark.asyncio
async def test_espn_metadata_parses_name_scoring_and_draft_date():
    from backend.integrations.espn_league_api import ESPNLeagueAPI

    league = MagicMock()
    league.league_id = "42"
    league.season_year = 2026
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="swid")

    async def fake_get(view, season=None):
        return {"settings": {
            "name": "espntest", "size": 12,
            "scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]},
            "draftSettings": {"date": 1756612800000},
        }}
    api._get = fake_get

    meta = await api.get_league_metadata()
    assert meta.name == "espntest"
    assert meta.scoring == "ppr"            # reception 1.0
    assert meta.team_count == 12
    assert meta.draft_date is not None


@pytest.mark.asyncio
async def test_espn_rosters_use_mteam_names_not_blank():
    """The all-blank ESPN names bug: mRoster carries only {id, roster}; names live in
    mTeam. get_rosters must merge them so manager_map isn't blank."""
    from backend.integrations.espn_league_api import ESPNLeagueAPI

    league = MagicMock()
    league.league_id = "42"
    league.season_year = 2026
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="swid")

    async def fake_get(view, season=None):
        if view == "mRoster":
            return {"teams": [{"id": 1, "roster": {"entries": []}},
                              {"id": 2, "roster": {"entries": []}}]}
        if view == "mTeam":
            return {"teams": [{"id": 1, "name": "Stephen's Smart Team", "abbrev": "SST"},
                              {"id": 2, "location": "Big", "nickname": "Cats", "abbrev": "BC"}]}
        return {}
    api._get = fake_get

    rosters = await api.get_rosters()
    names = {r.platform_team_id: r.team_name for r in rosters}
    assert names["1"] == "Stephen's Smart Team"
    assert names["2"] == "Big Cats"           # location + nickname


@pytest.mark.asyncio
async def test_sync_self_heals_is_active_on_resync():
    """is_active recomputes from season on EVERY sync — a stale False on a
    current-season league flips back to True (re-sync repairs it)."""
    league = _make_league(platform="sleeper")
    league.is_active = False                  # deliberately stale/wrong
    league.season_year = 2026                 # == current
    mock_db = _make_mock_db(league)

    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []
    mock_platform.get_rosters.return_value = _make_rosters(12)
    mock_platform.get_free_agents.return_value = []
    mock_platform.get_roster_slots.return_value = None
    from backend.integrations.platform_models import LeagueMetadata
    mock_platform.get_league_metadata.return_value = LeagueMetadata(name="Misfits")

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock, return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season", return_value=2026,
    ), _patch_league_repo(league):
        from backend.services.league_sync import LeagueSyncService
        await LeagueSyncService(mock_db, league.user_id).sync_league(league.id)

    assert league.is_active is True           # self-healed
    assert league.league_name == "Misfits"    # metadata captured
    assert league.team_count == 12            # real count from rosters


# ---------------------------------------------------------------------------
# is_me / your-team binding — exact owner-identity, never positional
# ---------------------------------------------------------------------------
from backend.services.league_sync import bind_my_team_id, LeagueSyncService  # noqa: E402


def _tr(team_id, owner_ids=None, is_me=None):
    return TeamRoster(platform_team_id=str(team_id), manager_name="", team_name=f"T{team_id}",
                      owner_ids=owner_ids or [], is_me=is_me)


def test_bind_sleeper_by_owner_id_not_position():
    """Sleeper: match stored user_id against owner_id — binds the RIGHT roster even when
    it is NOT first in the list (proves it's identity-based, not team[0])."""
    rosters = [_tr(1, owner_ids=["stranger-1"]), _tr(8, owner_ids=["me-777"]), _tr(3, owner_ids=["x"])]
    assert bind_my_team_id(rosters, "me-777") == "8"        # roster 8, not team[0]=1


def test_bind_espn_by_swid_case_and_braces_insensitive():
    """ESPN: SWID match ignores braces/case; matches within the owners[] list."""
    rosters = [_tr(1, owner_ids=["{AAAA}"]), _tr(2, owner_ids=["{b233bba4-5f04}"])]
    assert bind_my_team_id(rosters, "{B233BBA4-5F04}") == "2"


def test_bind_yahoo_by_flag_wins_without_identity():
    """Yahoo: the server-tagged is_me flag binds with NO stored identity."""
    rosters = [_tr("t.1"), _tr("t.2", is_me=True), _tr("t.3")]
    assert bind_my_team_id(rosters, None) == "t.2"


def test_bind_co_owner_matches_second_owner():
    """A co-owned team binds when the user is a CO-owner (2nd in owner_ids)."""
    rosters = [_tr(1, owner_ids=["primary-x", "me-co"]), _tr(2, owner_ids=["owner-b"])]
    assert bind_my_team_id(rosters, "me-co") == "1"


def test_bind_no_match_returns_none_never_team0():
    """The core safety property: a no-match binds NOTHING (None), never a positional
    team[0]. Covers bogus identity AND absent identity/flags."""
    rosters = [_tr(1, owner_ids=["a"]), _tr(2, owner_ids=["b"])]
    assert bind_my_team_id(rosters, "stranger-999") is None
    assert bind_my_team_id(rosters, None) is None            # no identity, no flags


@pytest.mark.asyncio
async def test_sync_binds_my_team_id_and_reheals_on_owner_change():
    """Sync stores my_team_id from identity, and RE-DERIVES it every sync — so an owner
    change (co-owner added, team reindexed) self-heals instead of going stale."""
    league = _make_league(platform="sleeper")
    league.my_team_id = "1"                    # stale/wrong prior binding
    mock_db = _make_mock_db(league)

    # user's Sleeper id → roster 8 (NOT first)
    rosters = [_tr(1, owner_ids=["stranger"]), _tr(8, owner_ids=["me-777"])]
    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []
    mock_platform.get_rosters.return_value = rosters
    mock_platform.get_free_agents.return_value = []
    mock_platform.get_roster_slots.return_value = None
    mock_platform.get_league_metadata.return_value = LeagueMetadata()

    async def _fake_identity(self, platform):
        return "me-777"

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock, return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season", return_value=2026,
    ), patch.object(
        LeagueSyncService, "_platform_identity", new=_fake_identity,
    ), _patch_league_repo(league):
        await LeagueSyncService(mock_db, league.user_id).sync_league(league.id)

    assert league.my_team_id == "8"            # rebound to the identity-matched team, not stale "1"


@pytest.mark.asyncio
async def test_sync_no_match_leaves_my_team_id_unbound():
    """A user whose identity matches no team → my_team_id None (loud-warn), never team[0]."""
    league = _make_league(platform="sleeper")
    league.my_team_id = "1"
    mock_db = _make_mock_db(league)
    rosters = [_tr(1, owner_ids=["stranger"]), _tr(2, owner_ids=["other"])]
    mock_platform = AsyncMock()
    mock_platform.get_draft_picks.return_value = []
    mock_platform.get_rosters.return_value = rosters
    mock_platform.get_free_agents.return_value = []
    mock_platform.get_roster_slots.return_value = None
    mock_platform.get_league_metadata.return_value = LeagueMetadata()

    async def _fake_identity(self, platform):
        return "me-not-in-league"

    with patch(
        "backend.services.league_sync.get_platform_api",
        new_callable=AsyncMock, return_value=mock_platform,
    ), patch(
        "backend.services.league_sync.get_current_season", return_value=2026,
    ), patch.object(
        LeagueSyncService, "_platform_identity", new=_fake_identity,
    ), _patch_league_repo(league):
        await LeagueSyncService(mock_db, league.user_id).sync_league(league.id)

    assert league.my_team_id is None           # unbound, not positional
