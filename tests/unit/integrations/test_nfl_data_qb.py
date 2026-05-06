"""Tests for QB and O-line stats functions in nfl_data.py."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from backend.integrations.nfl_data import (
    compute_qb_season_stats,
    compute_team_oline_stats,
)


def _mock_weekly_qb_data():
    """Minimal weekly data for QB stats testing."""
    return pd.DataFrame([
        {"player_id": "00-0033873", "player_name": "Patrick Mahomes",
         "recent_team": "KC", "position": "QB", "season_type": "REG",
         "week": 1, "completions": 25, "attempts": 35, "passing_yards": 300,
         "passing_tds": 3, "interceptions": 0, "sacks": 2, "sack_yards": 15,
         "rushing_yards": 20, "rushing_tds": 0, "carries": 3,
         "fantasy_points_ppr": 25.0, "season": 2024,
         "passing_air_yards": 280, "target_share": 0, "targets": 0,
         "receptions": 0, "receiving_yards": 0, "receiving_tds": 0,
         "air_yards_share": 0, "receiving_air_yards": 0, "dakota": 0.3},
        {"player_id": "00-0033873", "player_name": "Patrick Mahomes",
         "recent_team": "KC", "position": "QB", "season_type": "REG",
         "week": 2, "completions": 22, "attempts": 30, "passing_yards": 280,
         "passing_tds": 2, "interceptions": 1, "sacks": 1, "sack_yards": 8,
         "rushing_yards": 15, "rushing_tds": 1, "carries": 4,
         "fantasy_points_ppr": 22.0, "season": 2024,
         "passing_air_yards": 240, "target_share": 0, "targets": 0,
         "receptions": 0, "receiving_yards": 0, "receiving_tds": 0,
         "air_yards_share": 0, "receiving_air_yards": 0, "dakota": 0.25},
    ])


def _mock_ngs_passing():
    """Minimal NGS passing data."""
    return pd.DataFrame([
        {"player_gsis_id": "00-0033873", "player_display_name": "Patrick Mahomes",
         "team_abbr": "KC", "season": 2024, "season_type": "REG", "week": 0,
         "completion_percentage_above_expectation": 2.5,
         "avg_time_to_throw": 2.75, "aggressiveness": 15.2,
         "attempts": 500},
    ])


@pytest.fixture(autouse=True)
def _no_cache(tmp_path, monkeypatch):
    """Use temp dir for parquet cache so tests don't pollute."""
    monkeypatch.setattr("backend.integrations.nfl_data.CACHE_DIR", tmp_path)


def test_get_qb_season_stats_returns_expected_columns():
    """compute_qb_season_stats produces correct columns."""
    with patch("backend.integrations.nfl_data.fetch_weekly_stats", return_value=_mock_weekly_qb_data()):
        with patch("backend.integrations.nfl_data.fetch_ngs_data", return_value=_mock_ngs_passing()):
            result = compute_qb_season_stats(2024)

    assert not result.empty
    expected_cols = {"player_name", "games", "completions", "attempts",
                    "passing_yards", "passing_tds", "interceptions", "sacks",
                    "rushing_yards", "rushing_tds", "fantasy_points_ppr",
                    "completion_pct", "ppr_per_game", "cpoe", "avg_time_to_throw"}
    assert expected_cols.issubset(set(result.columns))

    row = result.iloc[0]
    assert row["player_name"] == "Patrick Mahomes"
    assert row["games"] == 2
    assert row["passing_yards"] == 580
    assert row["passing_tds"] == 5


def test_get_team_oline_stats_returns_expected_columns():
    """compute_team_oline_stats produces sack_rate and avg_time_to_throw."""
    with patch("backend.integrations.nfl_data.fetch_weekly_stats", return_value=_mock_weekly_qb_data()):
        with patch("backend.integrations.nfl_data.fetch_ngs_data", return_value=_mock_ngs_passing()):
            result = compute_team_oline_stats(2024)

    assert not result.empty
    expected_cols = {"team", "total_dropbacks", "total_sacks", "sack_rate", "avg_time_to_throw"}
    assert expected_cols.issubset(set(result.columns))

    row = result[result["team"] == "KC"].iloc[0]
    # 65 attempts + 3 sacks = 68 dropbacks, sack_rate = 3/68 ≈ 0.0441
    assert row["total_sacks"] == 3
    assert 0.04 < row["sack_rate"] < 0.05
    assert abs(row["avg_time_to_throw"] - 2.75) < 0.01


def test_qb_stats_merges_ngs_cpoe():
    """QB stats include CPOE from NGS data when available."""
    with patch("backend.integrations.nfl_data.fetch_weekly_stats", return_value=_mock_weekly_qb_data()):
        with patch("backend.integrations.nfl_data.fetch_ngs_data", return_value=_mock_ngs_passing()):
            result = compute_qb_season_stats(2024)

    row = result.iloc[0]
    assert row["cpoe"] == 2.5
    assert abs(row["avg_time_to_throw"] - 2.75) < 0.01
