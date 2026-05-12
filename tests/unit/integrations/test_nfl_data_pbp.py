"""Tests for PBP fallback stats computation in backend.integrations.nfl_data."""
from __future__ import annotations

import pickle
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backend.integrations.nfl_data import (
    _compute_target_share_from_pbp,
    compute_seasonal_stats_from_pbp,
    compute_target_share,
    get_seasonal_stats,
)


def _make_pbp_df(plays: list[dict]) -> pd.DataFrame:
    """Build a minimal PBP DataFrame from a list of play dicts."""
    base = {
        "season_type": "REG",
        "game_id": "2025_01_SF_ARI",
        "week": 1,
        "receiver_player_id": None,
        "receiver_player_name": None,
        "receiving_yards": None,
        "complete_pass": None,
        "pass_attempt": None,
        "touchdown": None,
        "rusher_player_id": None,
        "rusher_player_name": None,
        "rushing_yards": None,
        "passer_player_id": None,
        "passer_player_name": None,
        "passing_yards": None,
        "pass_touchdown": None,
        "interception": None,
        "fumble_lost": None,
        "fumbled_1_player_id": None,
        "fumbled_1_player_name": None,
    }
    rows = []
    for play in plays:
        row = {**base, **play}
        rows.append(row)
    return pd.DataFrame(rows)


MOCK_ROSTERS = pd.DataFrame({
    "player_id": ["00-001", "00-002"],
    "position": ["RB", "QB"],
    "team": ["SF", "BUF"],
})


@patch("backend.integrations.nfl_data.compute_seasonal_stats_from_pbp")
@patch("backend.integrations.nfl_data.nfl")
def test_get_seasonal_stats_falls_back_to_pbp(mock_nfl, mock_pbp_fn):
    """When import_weekly_data fails, compute_seasonal_stats_from_pbp is called."""
    mock_nfl.import_weekly_data.side_effect = Exception("HTTP Error 404")

    mock_result = pd.DataFrame({
        "player_id": ["00-001"],
        "player_display_name": ["C.McCaffrey"],
        "position": ["RB"],
        "recent_team": ["SF"],
        "games": [17],
        "fantasy_points_ppr": [414.6],
    })
    mock_pbp_fn.return_value = mock_result

    result = get_seasonal_stats(2025)

    mock_nfl.import_weekly_data.assert_called_once()
    mock_pbp_fn.assert_called_once_with(2025, "ppr")
    assert len(result) == 1
    assert result.iloc[0]["fantasy_points_ppr"] == 414.6


@patch("backend.integrations.nfl_data.nfl")
def test_pbp_stats_rushing_and_receiving(mock_nfl):
    """PBP computation correctly sums rushing + receiving + passing."""
    plays = [
        # Rush for 10 yards
        {
            "rusher_player_id": "00-001",
            "rusher_player_name": "C.McCaffrey",
            "rushing_yards": 10.0,
            "touchdown": 0,
        },
        # Catch for 20 yards (PPR = 1 + 2.0 yards = 3.0)
        {
            "receiver_player_id": "00-001",
            "receiver_player_name": "C.McCaffrey",
            "receiving_yards": 20.0,
            "complete_pass": 1,
            "pass_attempt": 1,
            "touchdown": 0,
            "passer_player_id": "00-002",
            "passer_player_name": "J.Allen",
            "passing_yards": 20.0,
            "pass_touchdown": 0,
            "interception": 0,
        },
        # Pass TD
        {
            "receiver_player_id": "00-003",
            "receiver_player_name": "S.Diggs",
            "receiving_yards": 40.0,
            "complete_pass": 1,
            "pass_attempt": 1,
            "touchdown": 1,
            "passer_player_id": "00-002",
            "passer_player_name": "J.Allen",
            "passing_yards": 40.0,
            "pass_touchdown": 1,
            "interception": 0,
        },
    ]
    mock_nfl.import_pbp_data.return_value = _make_pbp_df(plays)
    mock_nfl.import_seasonal_rosters.return_value = MOCK_ROSTERS

    result = compute_seasonal_stats_from_pbp(2025, use_cache=False)

    cmc = result[result["player_id"] == "00-001"].iloc[0]
    # Rush: 10 * 0.1 = 1.0
    # Rec: 1 (PPR) + 20 * 0.1 = 3.0
    # Total: 4.0
    assert cmc["fantasy_points_ppr"] == 4.0
    assert cmc["receptions"] == 1
    assert cmc["rushing_yards"] == 10
    assert cmc["receiving_yards"] == 20

    allen = result[result["player_id"] == "00-002"].iloc[0]
    # Pass play 1: 20 * 0.04 = 0.8
    # Pass play 2: 40 * 0.04 + 4 (TD) = 5.6
    # Total: 6.4
    assert abs(allen["fantasy_points_ppr"] - 6.4) < 0.01


@patch("backend.integrations.nfl_data.nfl")
def test_pbp_cache_used_on_second_call(mock_nfl, tmp_path):
    """Cached pickle is loaded on second call without recomputing."""
    plays = [
        {
            "rusher_player_id": "00-001",
            "rusher_player_name": "C.McCaffrey",
            "rushing_yards": 100.0,
            "touchdown": 1,
        },
    ]
    mock_nfl.import_pbp_data.return_value = _make_pbp_df(plays)
    mock_nfl.import_seasonal_rosters.return_value = MOCK_ROSTERS

    # First call computes fresh
    with patch("backend.integrations.nfl_data.CACHE_DIR", tmp_path):
        result1 = compute_seasonal_stats_from_pbp(2025, use_cache=True)
        assert mock_nfl.import_pbp_data.call_count == 1

        # Second call should use cache
        result2 = compute_seasonal_stats_from_pbp(2025, use_cache=True)
        # PBP should NOT be called again
        assert mock_nfl.import_pbp_data.call_count == 1

    assert len(result1) == len(result2)


@patch("backend.integrations.nfl_data.nfl")
def test_position_joined_from_rosters(mock_nfl):
    """Position column comes from seasonal rosters, not PBP data."""
    plays = [
        {
            "rusher_player_id": "00-001",
            "rusher_player_name": "C.McCaffrey",
            "rushing_yards": 50.0,
            "touchdown": 0,
        },
    ]
    mock_nfl.import_pbp_data.return_value = _make_pbp_df(plays)
    mock_nfl.import_seasonal_rosters.return_value = MOCK_ROSTERS

    result = compute_seasonal_stats_from_pbp(2025, use_cache=False)

    cmc = result[result["player_id"] == "00-001"].iloc[0]
    assert cmc["position"] == "RB"
    assert cmc["recent_team"] == "SF"


@patch("backend.integrations.nfl_data.nfl")
def test_pbp_handles_nan_yards(mock_nfl):
    """NaN yard values are treated as 0, not propagated."""
    plays = [
        {
            "passer_player_id": "00-002",
            "passer_player_name": "J.Allen",
            "passing_yards": float("nan"),
            "pass_touchdown": 1,
            "interception": 0,
        },
    ]
    mock_nfl.import_pbp_data.return_value = _make_pbp_df(plays)
    mock_nfl.import_seasonal_rosters.return_value = MOCK_ROSTERS

    result = compute_seasonal_stats_from_pbp(2025, use_cache=False)

    allen = result[result["player_id"] == "00-002"].iloc[0]
    # NaN yards → 0 * 0.04 = 0, plus TD = 4.0
    assert allen["fantasy_points_ppr"] == 4.0
    assert allen["passing_yards"] == 0


@patch("backend.integrations.nfl_data.nfl")
def test_fumble_lost_deduction(mock_nfl):
    """Fumble lost deducts 2 points from fantasy score."""
    plays = [
        {
            "rusher_player_id": "00-001",
            "rusher_player_name": "C.McCaffrey",
            "rushing_yards": 50.0,
            "touchdown": 0,
            "fumble_lost": 1,
            "fumbled_1_player_id": "00-001",
            "fumbled_1_player_name": "C.McCaffrey",
        },
    ]
    mock_nfl.import_pbp_data.return_value = _make_pbp_df(plays)
    mock_nfl.import_seasonal_rosters.return_value = MOCK_ROSTERS

    result = compute_seasonal_stats_from_pbp(2025, use_cache=False)

    cmc = result[result["player_id"] == "00-001"].iloc[0]
    # 50 yards * 0.1 = 5.0, minus fumble = 3.0
    assert cmc["fantasy_points_ppr"] == 3.0
    assert cmc["fumbles_lost"] == 1


# ---------------------------------------------------------------------------
# FIX 1: compute_target_share PBP fallback tests
# ---------------------------------------------------------------------------


@patch("backend.integrations.nfl_data.compute_seasonal_stats_from_pbp")
@patch("backend.integrations.nfl_data.nfl")
def test_compute_target_share_falls_back_to_pbp(mock_nfl, mock_pbp_fn, tmp_path):
    """When fetch_weekly_stats fails, compute_target_share falls back to PBP."""
    mock_nfl.import_weekly_data.side_effect = Exception("HTTP Error 404")

    mock_result = pd.DataFrame({
        "player_id": ["00-001", "00-002"],
        "player_display_name": ["C.McCaffrey", "D.Samuel"],
        "position": ["RB", "WR"],
        "recent_team": ["SF", "SF"],
        "games": [17, 16],
        "targets": [80, 120],
        "receptions": [65, 95],
        "receiving_yards": [550, 1100],
        "receiving_tds": [3, 7],
        "rush_attempts": [270, 15],
        "rushing_yards": [1200, 80],
        "rushing_tds": [10, 0],
        "fantasy_points_ppr": [414.6, 310.0],
        "season": [2025, 2025],
        "passing_yards": [0, 0],
        "passing_tds": [0, 0],
        "interceptions": [0, 0],
        "fumbles_lost": [0, 0],
    })
    mock_pbp_fn.return_value = mock_result

    with patch("backend.integrations.nfl_data.CACHE_DIR", tmp_path):
        result = compute_target_share(2025)

    assert len(result) == 2
    # Columns must match standard target_share output schema
    assert "player_name" in result.columns
    assert "total_targets" in result.columns
    assert "avg_target_share" in result.columns
    assert "total_carries" in result.columns
    assert "ppr_per_game" in result.columns

    cmc = result[result["player_id"] == "00-001"].iloc[0]
    assert cmc["total_targets"] == 80
    assert cmc["total_carries"] == 270
    assert abs(cmc["ppr_per_game"] - 414.6 / 17) < 0.1


@patch("backend.integrations.nfl_data.compute_seasonal_stats_from_pbp")
@patch("backend.integrations.nfl_data.nfl")
def test_target_share_pbp_fallback_computes_share(mock_nfl, mock_pbp_fn):
    """PBP fallback correctly computes target share as player_targets / team_targets."""
    mock_nfl.import_weekly_data.side_effect = Exception("HTTP Error 404")

    mock_result = pd.DataFrame({
        "player_id": ["00-001", "00-002"],
        "player_display_name": ["Player A", "Player B"],
        "position": ["WR", "WR"],
        "recent_team": ["SF", "SF"],
        "games": [17, 17],
        "targets": [150, 50],
        "receptions": [100, 30],
        "receiving_yards": [1200, 400],
        "receiving_tds": [10, 2],
        "rush_attempts": [0, 0],
        "rushing_yards": [0, 0],
        "rushing_tds": [0, 0],
        "fantasy_points_ppr": [300, 100],
        "season": [2025, 2025],
        "passing_yards": [0, 0],
        "passing_tds": [0, 0],
        "interceptions": [0, 0],
        "fumbles_lost": [0, 0],
    })
    mock_pbp_fn.return_value = mock_result

    result = _compute_target_share_from_pbp(2025)

    a = result[result["player_id"] == "00-001"].iloc[0]
    b = result[result["player_id"] == "00-002"].iloc[0]
    # Team total targets = 150 + 50 = 200
    assert abs(a["avg_target_share"] - 0.75) < 0.01
    assert abs(b["avg_target_share"] - 0.25) < 0.01
