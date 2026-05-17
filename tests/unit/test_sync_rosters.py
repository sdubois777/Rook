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

    with patch(
        "backend.integrations.sleeper.fetch_sleeper_players",
        return_value=sleeper_df,
    ):
        from scripts.sync_rosters import sync_players_from_sleeper
        result = await sync_players_from_sleeper(db=session)

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
