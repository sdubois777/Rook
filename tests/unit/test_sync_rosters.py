"""Tests for sync_players_from_sleeper() cache invalidation."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


def _make_player(
    name="Test Player",
    position="WR",
    team_abbr="ATL",
    sportradar_id=None,
    sleeper_id=None,
    gsis_id=None,
    depth_chart_order=None,
):
    """Create a mock Player ORM object."""
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = name
    p.position = position
    p.team_abbr = team_abbr
    p.sportradar_id = sportradar_id or str(uuid.uuid4())
    p.sleeper_id = sleeper_id
    p.gsis_id = gsis_id
    p.age = 25
    p.nfl_seasons_played = 3
    p.team_updated_at = None
    p.depth_chart_order = depth_chart_order
    return p


def _make_sleeper_df(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame mimicking fetch_sleeper_players() output."""
    defaults = {
        "player_id": "99999",
        "full_name": "Test Player",
        "position": "WR",
        "team": "ATL",
        "sportradar_id": str(uuid.uuid4()),
        "gsis_id": None,
        "age": 25,
        "years_exp": 3,
        "depth_chart_order": None,
    }
    data = []
    for row in rows:
        d = {**defaults, **row}
        data.append(d)
    return pd.DataFrame(data)


def _mock_session(existing_players: list):
    """Create a mock async session that returns existing_players on SELECT."""
    session = AsyncMock()

    # execute() returns a result with scalars().all() → existing_players
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = existing_players
    session.execute = AsyncMock(return_value=mock_result)

    session.commit = AsyncMock()
    session.add = MagicMock()

    # Track delete calls for cache invalidation assertions
    session._delete_calls = []
    _orig_execute = session.execute

    async def tracking_execute(stmt, *args, **kwargs):
        # Check if this is a DELETE statement (for AgentCache)
        stmt_str = str(stmt) if hasattr(stmt, 'compile') else str(stmt)
        if "DELETE" in str(type(stmt).__name__).upper() or "delete" in stmt_str.lower():
            session._delete_calls.append(stmt)
            return MagicMock()
        return await _orig_execute(stmt, *args, **kwargs)

    # Replace execute after first call (SELECT)
    call_count = {"n": 0}
    orig = session.execute

    async def smart_execute(stmt, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call is SELECT Player
            return mock_result
        # Subsequent calls are DELETE AgentCache
        session._delete_calls.append(stmt)
        return MagicMock()

    session.execute = smart_execute
    return session


@pytest.mark.asyncio
async def test_team_change_invalidates_old_team_cache():
    """Player moving from ATL to BUF invalidates ATL cache."""
    player = _make_player(
        name="John Doe", position="WR", team_abbr="ATL",
        sportradar_id="sr-123",
    )
    sleeper_df = _make_sleeper_df([{
        "player_id": "100",
        "full_name": "John Doe",
        "position": "WR",
        "team": "BUF",
        "sportradar_id": "sr-123",
    }])

    session = _mock_session([player])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    assert "ATL" in result["teams_invalidated"]
    assert result["cache_cleared"] is True


@pytest.mark.asyncio
async def test_team_change_invalidates_new_team_cache():
    """Player moving from ATL to BUF invalidates BUF cache."""
    player = _make_player(
        name="John Doe", position="WR", team_abbr="ATL",
        sportradar_id="sr-123",
    )
    sleeper_df = _make_sleeper_df([{
        "player_id": "100",
        "full_name": "John Doe",
        "position": "WR",
        "team": "BUF",
        "sportradar_id": "sr-123",
    }])

    session = _mock_session([player])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    assert "BUF" in result["teams_invalidated"]
    assert "ATL" in result["teams_invalidated"]


@pytest.mark.asyncio
async def test_depth_chart_change_invalidates_team():
    """Depth chart order change invalidates team cache."""
    player = _make_player(
        name="Jane Smith", position="RB", team_abbr="KC",
        sportradar_id="sr-456", depth_chart_order=2,
    )
    sleeper_df = _make_sleeper_df([{
        "player_id": "200",
        "full_name": "Jane Smith",
        "position": "RB",
        "team": "KC",
        "sportradar_id": "sr-456",
        "depth_chart_order": 1,  # promoted to starter
    }])

    session = _mock_session([player])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    assert "KC" in result["teams_invalidated"]
    assert player.depth_chart_order == 1


@pytest.mark.asyncio
async def test_id_update_only_does_not_invalidate():
    """Adding sleeper_id to existing player is not a meaningful change."""
    player = _make_player(
        name="Bob Jones", position="TE", team_abbr="NYG",
        sportradar_id="sr-789", sleeper_id=None,
    )
    sleeper_df = _make_sleeper_df([{
        "player_id": "300",
        "full_name": "Bob Jones",
        "position": "TE",
        "team": "NYG",  # same team
        "sportradar_id": "sr-789",
    }])

    session = _mock_session([player])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    assert result["teams_invalidated"] == []
    assert result["cache_cleared"] is False
    # IDs still updated
    assert player.sleeper_id == "300"


@pytest.mark.asyncio
async def test_no_changes_leaves_cache_intact():
    """No roster changes = cache fully preserved."""
    player = _make_player(
        name="Same Guy", position="QB", team_abbr="SF",
        sportradar_id="sr-000", sleeper_id="400",
        depth_chart_order=1,
    )
    sleeper_df = _make_sleeper_df([{
        "player_id": "400",
        "full_name": "Same Guy",
        "position": "QB",
        "team": "SF",  # same team
        "sportradar_id": "sr-000",
        "depth_chart_order": 1,  # same depth
    }])

    session = _mock_session([player])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    assert result["teams_invalidated"] == []
    assert result["cache_cleared"] is False
    assert result["updated"] == 1


@pytest.mark.asyncio
async def test_new_player_invalidates_their_team():
    """Inserting a new player invalidates their team's cache."""
    sleeper_df = _make_sleeper_df([{
        "player_id": "500",
        "full_name": "Rookie Star",
        "position": "WR",
        "team": "LAC",
        "sportradar_id": "sr-new",
    }])

    session = _mock_session([])  # no existing players

    # Pass a warehouse where the new player is relevant (on the depth chart),
    # so the recent-activity gate inserts them.
    wh = _gate_warehouse(depth_sleeper_ids=["500"])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session, warehouse=wh)

    assert "LAC" in result["teams_invalidated"]
    assert result["inserted"] == 1
    assert result["cache_cleared"] is True


@pytest.mark.asyncio
async def test_sync_returns_teams_invalidated():
    """Return dict includes sorted teams_invalidated list."""
    p1 = _make_player(
        name="Player A", position="WR", team_abbr="NYJ",
        sportradar_id="sr-a",
    )
    p2 = _make_player(
        name="Player B", position="RB", team_abbr="DEN",
        sportradar_id="sr-b",
    )
    sleeper_df = _make_sleeper_df([
        {
            "player_id": "601",
            "full_name": "Player A",
            "position": "WR",
            "team": "MIA",  # NYJ -> MIA
            "sportradar_id": "sr-a",
        },
        {
            "player_id": "602",
            "full_name": "Player B",
            "position": "RB",
            "team": "GB",  # DEN -> GB
            "sportradar_id": "sr-b",
        },
    ])

    session = _mock_session([p1, p2])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    # 4 teams: NYJ, MIA (player A), DEN, GB (player B)
    assert result["teams_invalidated"] == sorted(["DEN", "GB", "MIA", "NYJ"])
    assert result["cache_cleared"] is True
    assert result["updated"] == 2


# ===========================================================================
# Suffix normalization tests (Jr./Sr./II/III)
# ===========================================================================


def test_suffix_normalization():
    """_SUFFIX_RE strips Jr., Sr., II, III, IV, V from player names."""
    from scripts.sync_rosters import _SUFFIX_RE

    cases = {
        "Brian Thomas Jr.": "Brian Thomas",
        "Brian Thomas Jr": "Brian Thomas",
        "Kenneth Walker III": "Kenneth Walker",
        "Marvin Harrison Jr.": "Marvin Harrison",
        "Odell Beckham Jr": "Odell Beckham",
        "Anthony Tyus Jr.": "Anthony Tyus",
        "Michael Pittman Jr": "Michael Pittman",
        "Irv Smith Jr.": "Irv Smith",
        "Patrick Mahomes II": "Patrick Mahomes",
    }
    for original, expected in cases.items():
        stripped = _SUFFIX_RE.sub("", original).strip()
        assert stripped == expected, f"Failed: {original!r} → {stripped!r}, expected {expected!r}"


@pytest.mark.asyncio
async def test_jr_sr_suffix_handled_in_sync():
    """Sleeper sends 'Brian Thomas' (no suffix) → matches DB 'Brian Thomas Jr.'."""
    # DB has "Brian Thomas Jr." with sportradar_id but no sleeper_id
    player = _make_player(
        name="Brian Thomas Jr.",
        position="WR",
        team_abbr="JAX",
        sportradar_id=None,
        sleeper_id=None,
    )

    # Sleeper sends "Brian Thomas" (no suffix) with a sleeper_id
    sleeper_df = _make_sleeper_df([{
        "player_id": "12345",
        "full_name": "Brian Thomas",
        "position": "WR",
        "team": "JAX",
        "sportradar_id": None,
    }])

    session = _mock_session([player])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    # Should match (update), NOT insert a duplicate
    assert result["updated"] == 1
    assert result["inserted"] == 0
    # sleeper_id should be set on the matched player
    assert player.sleeper_id == "12345"


@pytest.mark.asyncio
async def test_no_brian_thomas_duplicate():
    """Syncing 'Brian Thomas' from Sleeper when DB has 'Brian Thomas Jr.' must not create a new record."""
    player_jr = _make_player(
        name="Brian Thomas Jr.",
        position="WR",
        team_abbr="JAX",
        sportradar_id="sr-btj",
    )

    sleeper_df = _make_sleeper_df([{
        "player_id": "99999",
        "full_name": "Brian Thomas",
        "position": "WR",
        "team": "JAX",
        "sportradar_id": "sr-btj",
    }])

    session = _mock_session([player_jr])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    assert result["inserted"] == 0
    assert result["updated"] == 1


@pytest.mark.asyncio
async def test_brian_thomas_jr_gets_sleeper_id():
    """After sync, Brian Thomas Jr. should have the sleeper_id from the suffix-less Sleeper entry."""
    player_jr = _make_player(
        name="Brian Thomas Jr.",
        position="WR",
        team_abbr="JAX",
        sportradar_id=None,
        sleeper_id=None,
    )

    sleeper_df = _make_sleeper_df([{
        "player_id": "BTJ_SLEEPER",
        "full_name": "Brian Thomas",
        "position": "WR",
        "team": "JAX",
        "sportradar_id": "sr-btj-new",
    }])

    session = _mock_session([player_jr])

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

    assert player_jr.sleeper_id == "BTJ_SLEEPER"
    assert player_jr.sportradar_id == "sr-btj-new"


# ---------------------------------------------------------------------------
# Recent-activity gate — is_relevant_player()
# ---------------------------------------------------------------------------
import types  # noqa: E402

from scripts.sync_rosters import is_relevant_player  # noqa: E402


def _gate_warehouse(depth_sleeper_ids=(), stats_gsis_by_season=None):
    """Mock warehouse: a 2026 depth-chart frame (sleeper_id keyed) and
    per-season seasonal-stats frames (player_id == gsis)."""
    stats_gsis_by_season = stats_gsis_by_season or {}
    dc = pd.DataFrame({"sleeper_id": [str(s) for s in depth_sleeper_ids]})
    frames = {
        season: pd.DataFrame({"player_id": [str(g) for g in gsis_list]})
        for season, gsis_list in stats_gsis_by_season.items()
    }
    return types.SimpleNamespace(
        depth_charts={2026: dc},
        get_seasonal_stats=lambda s: frames.get(s, pd.DataFrame()),
    )


def test_roethlisberger_filtered_at_sync():
    """No 2026 depth chart slot, no 2024/2025 games -> filtered out."""
    wh = _gate_warehouse(depth_sleeper_ids=["4046"], stats_gsis_by_season={2025: ["00-0033873"]})
    ben = {"player_id": "138", "gsis_id": "00-0022924", "full_name": "Ben Roethlisberger"}
    assert is_relevant_player(ben, wh) is False


def test_mahomes_kept_at_sync():
    """A 2025 game appearance (gsis in stats) keeps the player."""
    wh = _gate_warehouse(depth_sleeper_ids=[], stats_gsis_by_season={2025: ["00-0033873"]})
    mahomes = {"player_id": "4046", "gsis_id": "00-0033873", "full_name": "Patrick Mahomes"}
    assert is_relevant_player(mahomes, wh) is True


def test_ir_player_kept_via_depth_chart():
    """On the 2026 depth chart but no recent games (IR starter) -> kept."""
    wh = _gate_warehouse(depth_sleeper_ids=["999"], stats_gsis_by_season={2025: []})
    ir = {"player_id": "999", "gsis_id": "00-0099999", "full_name": "Hurt Starter"}
    assert is_relevant_player(ir, wh) is True


def test_gsis_matching_not_sleeper_id():
    """Stats are matched by gsis, not sleeper_id. A player whose sleeper_id
    coincidentally equals a stats player_id (gsis) is NOT matched."""
    # stats frame keyed by gsis "G-REAL"; player has sleeper_id 'G-REAL' but a
    # different gsis -> must NOT match (proves gsis-keyed, not sleeper-keyed).
    wh = _gate_warehouse(depth_sleeper_ids=[], stats_gsis_by_season={2025: ["G-REAL"]})
    collider = {"player_id": "G-REAL", "gsis_id": "G-OTHER", "full_name": "Collision Guy"}
    assert is_relevant_player(collider, wh) is False
    # Same player, correct gsis -> matches.
    matched = {"player_id": "12345", "gsis_id": "G-REAL", "full_name": "Real Guy"}
    assert is_relevant_player(matched, wh) is True


def test_no_gsis_and_no_depth_chart_filtered():
    """A player with no gsis and no depth-chart slot (UDFA noise) -> filtered."""
    wh = _gate_warehouse(depth_sleeper_ids=["111"], stats_gsis_by_season={2025: ["00-0011111"]})
    udfa = {"player_id": "55555", "gsis_id": "", "full_name": "Practice Squad Guy"}
    assert is_relevant_player(udfa, wh) is False
