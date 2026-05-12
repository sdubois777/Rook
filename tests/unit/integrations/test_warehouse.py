"""
tests/unit/integrations/test_warehouse.py

Verifies the NflDataWarehouse centralized data loader and confirms
no agent has its own _data_cache.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from backend.integrations.nfl_data import NflDataWarehouse


# ---------------------------------------------------------------------------
# Test: NflDataWarehouse builds without error
# ---------------------------------------------------------------------------

def test_warehouse_builds():
    """NflDataWarehouse.build() returns a warehouse with expected attributes."""
    with patch("backend.integrations.nfl_data.get_seasonal_stats") as mock_stats, \
         patch("backend.integrations.nfl_data.compute_target_share") as mock_ts, \
         patch("backend.integrations.nfl_data.compute_qb_season_stats") as mock_qb, \
         patch("backend.integrations.nfl_data.compute_team_oline_stats") as mock_oline, \
         patch("backend.integrations.nfl_data.fetch_weekly_stats") as mock_weekly, \
         patch("backend.agents.schedule.compute_def_grades") as mock_def, \
         patch("backend.integrations.nfl_data.fetch_injuries") as mock_inj, \
         patch("backend.integrations.nfl_data.fetch_rosters") as mock_rosters, \
         patch("backend.integrations.nfl_data.fetch_seasonal_rosters") as mock_sr, \
         patch("backend.integrations.nfl_data.fetch_schedules") as mock_sched, \
         patch("backend.integrations.nfl_data.fetch_ngs_data") as mock_ngs, \
         patch("backend.integrations.nfl_data.compute_snap_pct") as mock_snap:
        # Return empty DataFrames for all
        for m in (mock_stats, mock_ts, mock_qb, mock_oline, mock_weekly, mock_def,
                  mock_inj, mock_rosters, mock_sr, mock_sched, mock_ngs, mock_snap):
            m.return_value = pd.DataFrame()

        warehouse = NflDataWarehouse.build()

    assert warehouse is not None
    assert hasattr(warehouse, "rosters")
    assert hasattr(warehouse, "schedule")
    assert hasattr(warehouse, "schedule_year")
    assert callable(getattr(warehouse, "get_seasonal_stats", None))
    assert callable(getattr(warehouse, "get_target_share", None))
    assert callable(getattr(warehouse, "get_qb_stats", None))
    assert callable(getattr(warehouse, "get_oline_stats", None))
    assert callable(getattr(warehouse, "get_injuries", None))
    assert callable(getattr(warehouse, "get_most_recent_def_grades", None))
    assert callable(getattr(warehouse, "get_snap_pct", None))
    assert callable(getattr(warehouse, "get_ngs_receiving", None))
    assert callable(getattr(warehouse, "get_ngs_rushing", None))
    assert callable(getattr(warehouse, "summary", None))


# ---------------------------------------------------------------------------
# Test: missing season returns empty DataFrame
# ---------------------------------------------------------------------------

def test_warehouse_missing_season_returns_empty_df():
    """Accessor methods return empty DataFrame for seasons not loaded."""
    wh = NflDataWarehouse.__new__(NflDataWarehouse)
    wh.seasonal_stats = {}
    wh.target_share = {}
    wh.qb_stats = {}
    wh.oline_stats = {}
    wh.def_grades = {}
    wh.injuries = {}
    wh.ngs_receiving = {}
    wh.ngs_rushing = {}
    wh.snap_pct = {}
    wh.rosters = pd.DataFrame()
    wh.seasonal_rosters = pd.DataFrame()
    wh.prev_rosters = pd.DataFrame()
    wh.schedule = pd.DataFrame()
    wh.schedule_year = 2026

    assert wh.get_seasonal_stats(9999).empty
    assert wh.get_target_share(9999).empty
    assert wh.get_qb_stats(9999).empty
    assert wh.get_oline_stats(9999).empty
    assert wh.get_injuries(9999).empty
    assert wh.get_snap_pct(9999).empty
    assert wh.get_ngs_receiving(9999).empty
    assert wh.get_ngs_rushing(9999).empty


# ---------------------------------------------------------------------------
# Test: no _data_cache in any agent
# ---------------------------------------------------------------------------

_AGENT_MODULES = [
    "backend/agents/team_systems.py",
    "backend/agents/injury_risk.py",
    "backend/agents/schedule.py",
    "backend/agents/player_profiles.py",
    "backend/agents/roster_changes.py",
]


@pytest.mark.parametrize("module_path", _AGENT_MODULES)
def test_no_data_cache_in_agents(module_path):
    """No agent file should contain _data_cache or _DATA_CACHE."""
    source = (Path(__file__).parent.parent.parent.parent / module_path).read_text()
    assert "_data_cache" not in source, f"{module_path} still references _data_cache"
    assert "_DATA_CACHE" not in source, f"{module_path} still references _DATA_CACHE"


# ---------------------------------------------------------------------------
# Test: agents use warehouse, not cache
# ---------------------------------------------------------------------------

def test_team_systems_uses_warehouse_not_cache():
    """TeamSystemsAgent has _warehouse attribute from BaseAgent."""
    from backend.agents.team_systems import TeamSystemsAgent
    agent = TeamSystemsAgent()
    assert hasattr(agent, "_warehouse")
    assert not hasattr(type(agent), "_data_cache")


def test_injury_risk_uses_warehouse_not_cache():
    """InjuryRiskAgent has _warehouse attribute from BaseAgent."""
    from backend.agents.injury_risk import InjuryRiskAgent
    agent = InjuryRiskAgent()
    assert hasattr(agent, "_warehouse")
    assert not hasattr(type(agent), "_data_cache")


def test_schedule_uses_warehouse_not_cache():
    """ScheduleAgent has _warehouse attribute from BaseAgent."""
    from backend.agents.schedule import ScheduleAgent
    agent = ScheduleAgent()
    assert hasattr(agent, "_warehouse")
    assert not hasattr(type(agent), "_data_cache")


def test_player_profiles_uses_warehouse_not_cache():
    """PlayerProfilesAgent has _warehouse attribute from BaseAgent."""
    from backend.agents.player_profiles import PlayerProfilesAgent
    agent = PlayerProfilesAgent()
    assert hasattr(agent, "_warehouse")
    assert not hasattr(type(agent), "_data_cache")


def test_roster_changes_uses_warehouse_not_cache():
    """RosterChangesAgent has _warehouse attribute from BaseAgent."""
    from backend.agents.roster_changes import RosterChangesAgent
    agent = RosterChangesAgent()
    assert hasattr(agent, "_warehouse")
    assert not hasattr(type(agent), "_data_cache")


# ---------------------------------------------------------------------------
# Test: warehouse summary
# ---------------------------------------------------------------------------

def test_warehouse_summary():
    """summary() returns a dict with data counts."""
    wh = NflDataWarehouse.__new__(NflDataWarehouse)
    wh.analysis_seasons = [2024]
    wh.current_season = 2025
    wh.analysis_year = 2026
    wh.seasonal_stats = {2024: pd.DataFrame({"a": [1]})}
    wh.target_share = {2024: pd.DataFrame({"a": [1]})}
    wh.qb_stats = {}
    wh.oline_stats = {}
    wh.def_grades = {}
    wh.injuries = {}
    wh.ngs_receiving = {}
    wh.ngs_rushing = {}
    wh.snap_pct = {}
    wh.rosters = pd.DataFrame({"a": [1, 2]})
    wh.seasonal_rosters = pd.DataFrame()
    wh.prev_rosters = pd.DataFrame()
    wh.schedule = pd.DataFrame()
    wh.schedule_year = 2026

    result = wh.summary()
    assert isinstance(result, dict)
    assert "seasons_loaded" in result or len(result) > 0


# ---------------------------------------------------------------------------
# Test: oline PBP fallback
# ---------------------------------------------------------------------------

def test_oline_pbp_fallback_returns_dataframe():
    """_compute_oline_from_pbp returns a DataFrame with sack_rate column."""
    from backend.integrations.nfl_data import _compute_oline_from_pbp

    pbp_data = pd.DataFrame({
        "season_type": ["REG"] * 100,
        "posteam": ["LAC"] * 50 + ["KC"] * 50,
        "pass_attempt": [1] * 60 + [0] * 40,
        "sack": [0] * 90 + [1] * 10,
    })

    with patch("nfl_data_py.import_pbp_data", return_value=pbp_data), \
         patch("pathlib.Path.exists", return_value=False), \
         patch.object(pd.DataFrame, "to_parquet"):
        result = _compute_oline_from_pbp(2025)

    assert not result.empty
    assert "sack_rate" in result.columns
    assert "team" in result.columns
