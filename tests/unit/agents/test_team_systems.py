"""
tests/unit/agents/test_team_systems.py

All required named test cases from stage-03-team-systems.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pandas as pd

from backend.agents.team_systems import TeamSystemsAgent, NFL_TEAMS


# ---------------------------------------------------------------------------
# Mock warehouse (replaces old _data_cache direct injection)
# ---------------------------------------------------------------------------

class _MockWarehouse:
    """Minimal warehouse substitute for tests."""
    def __init__(self, **kwargs):
        self._data = kwargs
        self.rosters = kwargs.get("rosters", pd.DataFrame())
        self.seasonal_rosters = kwargs.get("seasonal_rosters", pd.DataFrame())
        self.prev_rosters = kwargs.get("prev_rosters", pd.DataFrame())
        self.schedule = kwargs.get("schedule", pd.DataFrame())
        self.schedule_year = kwargs.get("schedule_year", 2026)

    def get_seasonal_stats(self, season):
        return self._data.get("seasonal_stats", {}).get(season, pd.DataFrame())
    def get_target_share(self, season):
        return self._data.get("target_share", {}).get(season, pd.DataFrame())
    def get_qb_stats(self, season):
        return self._data.get("qb_stats", {}).get(season, pd.DataFrame())
    def get_oline_stats(self, season):
        return self._data.get("oline_stats", {}).get(season, pd.DataFrame())
    def get_def_grades(self, season):
        return self._data.get("def_grades", {}).get(season, pd.DataFrame())
    def get_injuries(self, season):
        return self._data.get("injuries", {}).get(season, pd.DataFrame())
    def get_most_recent_def_grades(self):
        return self._data.get("most_recent_def_grades", pd.DataFrame())
    def get_snap_pct(self, season):
        return self._data.get("snap_pct", {}).get(season, pd.DataFrame())
    def get_ngs_receiving(self, season):
        return self._data.get("ngs_receiving", {}).get(season, pd.DataFrame())
    def get_ngs_rushing(self, season):
        return self._data.get("ngs_rushing", {}).get(season, pd.DataFrame())
    def get_depth_chart(self, season):
        return self._data.get("depth_charts", {}).get(season, pd.DataFrame())
    def get_starter(self, team, position, season=None):
        return self._data.get("starters", {}).get((team, position))
    def get_player_depth_rank(self, gsis_id, season=None):
        return self._data.get("depth_ranks", {}).get(gsis_id)
    def get_team_depth_context(self, team, season=None):
        return self._data.get("depth_context", {}).get(team, {})
    def summary(self):
        return {}


def _make_warehouse(**kwargs):
    return _MockWarehouse(**kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent():
    """TeamSystemsAgent instance with mocked DB and API."""
    return TeamSystemsAgent(dry_run=False)


@pytest.fixture
def mock_call_once():
    """Helper to mock call_once on an agent instance."""
    def _make(response: dict):
        return AsyncMock(return_value=json.dumps(response))
    return _make


def _minimal_team_system(team: str, **overrides) -> dict:
    base = {
        "team_abbr": team,
        "pass_protection_grade": "B",
        "run_blocking_grade": "B",
        "qb_name": "Joe Starter",
        "qb_tier": "solid",
        "qb_experience_years": 5,
        "qb_pressure_performance": "avg",
        "qb_cpoe": 1.0,
        "qb_air_yards_per_attempt": 7.5,
        "qb_downfield_aggressiveness": "moderate",
        "rookie_qb_flag": False,
        "compound_risk_flag": False,
        "oc_name": "John OC",
        "oc_scheme": "balanced",
        "oc_run_pass_split_tendency": 0.55,
        "personnel_tendency": "11",
        "red_zone_philosophy": "wr1",
        "system_ceiling": "moderate",
        "system_grade": "B",
        "notes": "Stable system, no major concerns.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Single API call enforcement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_api_call_per_team():
    """run_for_team() must call call_once() exactly ONCE — never more."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system("LAC")

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "LAC"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))) as mock_call:
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()):
                await agent.run_for_team("LAC")

    mock_call.assert_called_once()


# ---------------------------------------------------------------------------
# Rookie QB flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rookie_qb_flag_first_year_starter():
    """rookie_qb_flag=True must be preserved when model returns it true."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system("ATL", rookie_qb_flag=True, compound_risk_flag=False)

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "ATL"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()) as mock_upsert:
                result = await agent.run_for_team("ATL")

    assert result is not None
    assert result["rookie_qb_flag"] is True


@pytest.mark.asyncio
async def test_rookie_qb_flag_false_veteran():
    """rookie_qb_flag=False for a veteran QB."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system("KC", qb_name="Patrick Mahomes", qb_tier="elite",
                                     rookie_qb_flag=False, compound_risk_flag=False)

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "KC"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()):
                result = await agent.run_for_team("KC")

    assert result is not None
    assert result["rookie_qb_flag"] is False


# ---------------------------------------------------------------------------
# Compound risk flag
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compound_risk_flag_rookie_qb_bad_line():
    """compound_risk_flag=True when rookie QB AND pass protection C or below."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system(
        "ATL",
        rookie_qb_flag=True,
        compound_risk_flag=True,
        pass_protection_grade="C",
    )

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "ATL"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()):
                result = await agent.run_for_team("ATL")

    assert result["compound_risk_flag"] is True
    assert result["rookie_qb_flag"] is True


@pytest.mark.asyncio
async def test_compound_risk_flag_false_veteran_qb():
    """compound_risk_flag must be False when rookie_qb_flag is False."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system("KC", rookie_qb_flag=False, compound_risk_flag=False,
                                     pass_protection_grade="C")

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "KC"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()):
                result = await agent.run_for_team("KC")

    assert result["compound_risk_flag"] is False


@pytest.mark.asyncio
async def test_compound_risk_flag_false_rookie_qb_good_line():
    """compound_risk_flag=False when rookie QB but pass protection B or above."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system(
        "MIN",
        rookie_qb_flag=True,
        compound_risk_flag=False,
        pass_protection_grade="B+",
    )

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "MIN"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()):
                result = await agent.run_for_team("MIN")

    assert result["compound_risk_flag"] is False
    assert result["rookie_qb_flag"] is True


# ---------------------------------------------------------------------------
# O-line grades stored separately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oline_grades_stored_separately():
    """pass_protection_grade and run_blocking_grade must be separate fields."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system("BAL", pass_protection_grade="A-", run_blocking_grade="A")

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "BAL"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()):
                result = await agent.run_for_team("BAL")

    assert "pass_protection_grade" in result
    assert "run_blocking_grade" in result
    assert result["pass_protection_grade"] != result["run_blocking_grade"]


# ---------------------------------------------------------------------------
# No hardcoded years
# ---------------------------------------------------------------------------

def test_no_hardcoded_years():
    """Scan team_systems.py source for literal year integers."""
    source = (Path(__file__).parent.parent.parent.parent / "backend" / "agents" / "team_systems.py").read_text()
    year_re = re.compile(r"\b(202[2-9])\b")
    model_re = re.compile(r"claude-[a-z]+-[\d]+-[\d]+-\w+")

    violations = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        cleaned = model_re.sub("", line)
        if year_re.search(cleaned):
            violations.append(f"line {lineno}: {line.strip()}")

    assert not violations, "Hardcoded years in team_systems.py:\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# NFL_TEAMS list completeness
# ---------------------------------------------------------------------------

def test_all_32_teams_in_nfl_teams_list():
    """NFL_TEAMS must contain all 32 NFL team abbreviations."""
    assert len(NFL_TEAMS) == 32
    # Spot-check known teams
    for team in ("KC", "LAC", "SF", "BAL", "BUF", "NYJ", "WAS"):
        assert team in NFL_TEAMS, f"{team} missing from NFL_TEAMS"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_makes_no_api_calls():
    """With dry_run=True, call_once() must not call messages.create()."""
    agent = TeamSystemsAgent(dry_run=True)

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "LAC"})):
        with patch.object(agent._client, "messages") as mock_messages:
            mock_messages.create = AsyncMock()
            result = await agent.run_for_team("LAC")

    # dry_run returns None (no real output)
    assert result is None
    mock_messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# Cache hit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_skips_api_call():
    """
    When the agent_cache has a matching entry, call_once() returns cached
    output and never calls messages.create().
    """
    agent = TeamSystemsAgent()
    cached_output = json.dumps(_minimal_team_system("LAC"))

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "LAC"})):
        with patch.object(agent, "_check_cache", new=AsyncMock(return_value=cached_output)):
            with patch.object(agent, "_log_usage", new=AsyncMock()):
                with patch.object(agent._client, "messages") as mock_messages:
                    mock_messages.create = AsyncMock()
                    with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()):
                        result = await agent.run_for_team("LAC")

    mock_messages.create.assert_not_called()
    assert result is not None


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_output_written_to_team_systems_table():
    """run_for_team() must call _upsert_team_system() with the parsed data."""
    agent = TeamSystemsAgent()
    response = _minimal_team_system("LAC")

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value={"team": "LAC"})):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()) as mock_upsert:
                await agent.run_for_team("LAC")

    mock_upsert.assert_called_once()
    call_args = mock_upsert.call_args
    written_data = call_args[0][1]  # second positional arg is the data dict
    assert written_data["team_abbr"] == "LAC"


# ---------------------------------------------------------------------------
# O-line numerics and QB mobility tests
# ---------------------------------------------------------------------------


def test_qb_mobility_derived_from_rushing():
    """QB mobility is derived from rushing yards per game."""
    from backend.agents.team_systems import _derive_qb_mobility

    # Elite rusher (Lamar Jackson type)
    assert _derive_qb_mobility({"games_played": 17, "rushing_yards": 850}) == "elite"
    # Average (some scrambling)
    assert _derive_qb_mobility({"games_played": 17, "rushing_yards": 400}) == "average"
    # Pocket passer
    assert _derive_qb_mobility({"games_played": 17, "rushing_yards": 100}) == "pocket_only"
    # Not enough games
    assert _derive_qb_mobility({"games_played": 3, "rushing_yards": 200}) is None


@pytest.mark.asyncio
async def test_sack_rate_passed_to_upsert():
    """run_for_team attaches Python-computed sack_rate to upsert data."""
    from backend.agents.team_systems import _derive_qb_mobility

    agent = TeamSystemsAgent(dry_run=False)

    context = {
        "team": "KC",
        "oline": {"sack_rate": 0.0512, "avg_time_to_throw": 2.8},
        "qb_metrics": {"games_played": 17, "rushing_yards": 200},
        "personnel": {},
        "roster_summary": {},
    }
    response = _minimal_team_system("KC")

    with patch.object(agent, "_build_team_context", new=AsyncMock(return_value=context)):
        with patch.object(agent, "call_once", new=AsyncMock(return_value=json.dumps(response))):
            with patch("backend.agents.team_systems._upsert_team_system", new=AsyncMock()) as mock_upsert:
                await agent.run_for_team("KC")

    written_data = mock_upsert.call_args[0][1]
    assert written_data["_sack_rate"] == 0.0512
    assert written_data["_avg_time_to_throw"] == 2.8
    assert written_data["_qb_mobility"] == "pocket_only"


# ---------------------------------------------------------------------------
# Two-source QB identification tests
# ---------------------------------------------------------------------------

def _mock_seasonal_roster(entries: list[dict]) -> pd.DataFrame:
    """Build a minimal seasonal roster DataFrame."""
    return pd.DataFrame(entries)


def _mock_weekly_qb_rows(rows: list[dict]) -> pd.DataFrame:
    """Build a weekly stats DataFrame with QB rows and required columns."""
    defaults = {
        "position": "QB", "completions": 0, "attempts": 0,
        "passing_yards": 0, "passing_tds": 0, "interceptions": 0,
        "passing_air_yards": 0, "rushing_yards": 0, "rushing_tds": 0,
        "sacks": 0, "targets": 0, "dakota": None,
    }
    full_rows = []
    for r in rows:
        row = {**defaults, **r}
        full_rows.append(row)
    return pd.DataFrame(full_rows)


@pytest.mark.asyncio
async def test_qb_from_seasonal_roster_not_stats_leader():
    """Roster QB (Darnold) should be returned, not stats leader (Geno)."""
    agent = TeamSystemsAgent(dry_run=False)

    seasonal = _mock_seasonal_roster([
        {"team": "SEA", "position": "QB", "status": "ACT", "player_name": "Sam Darnold"},
    ])
    weekly = _mock_weekly_qb_rows([
        {"player_name": "Geno Smith", "recent_team": "SEA", "attempts": 500, "completions": 320, "passing_yards": 3600},
        {"player_name": "Sam Darnold", "recent_team": "MIN", "attempts": 450, "completions": 280, "passing_yards": 3200},
    ])

    from backend.utils.seasons import get_current_season
    current = get_current_season()
    agent._warehouse = _make_warehouse(
        seasonal_rosters=seasonal,
        qb_stats={current: weekly},
    )

    result = await agent._get_qb_data("SEA", current)

    assert result["starter_name"] == "Sam Darnold"
    assert result["source"] == "roster+stats"


@pytest.mark.asyncio
async def test_qb_stats_pulled_from_previous_team():
    """Stats should come from Darnold's MIN data even though he's now on SEA."""
    agent = TeamSystemsAgent(dry_run=False)

    seasonal = _mock_seasonal_roster([
        {"team": "SEA", "position": "QB", "status": "ACT", "player_name": "Sam Darnold"},
    ])
    weekly = _mock_weekly_qb_rows([
        {"player_name": "Geno Smith", "recent_team": "SEA", "attempts": 500, "completions": 320, "passing_yards": 3600},
        {"player_name": "Sam Darnold", "recent_team": "MIN", "attempts": 450, "completions": 280, "passing_yards": 3200, "passing_tds": 25},
    ])

    from backend.utils.seasons import get_current_season
    current = get_current_season()
    agent._warehouse = _make_warehouse(
        seasonal_rosters=seasonal,
        qb_stats={current: weekly},
    )

    result = await agent._get_qb_data("SEA", current)

    assert result["starter_name"] == "Sam Darnold"
    assert result["passing_yards"] == 3200
    assert result["passing_tds"] == 25
    assert result["total_attempts"] == 450


@pytest.mark.asyncio
async def test_qb_fallback_when_no_seasonal_roster():
    """Without seasonal roster, fall back to most-attempts on team."""
    agent = TeamSystemsAgent(dry_run=False)

    weekly = _mock_weekly_qb_rows([
        {"player_name": "Geno Smith", "recent_team": "SEA", "attempts": 500, "completions": 320, "passing_yards": 3600},
        {"player_name": "Drew Lock", "recent_team": "SEA", "attempts": 50, "completions": 28, "passing_yards": 400},
    ])

    from backend.utils.seasons import get_current_season
    current = get_current_season()
    # No seasonal roster — should fall back to stats leader
    agent._warehouse = _make_warehouse(
        qb_stats={current: weekly},
    )

    result = await agent._get_qb_data("SEA", current)

    assert result["starter_name"] == "Geno Smith"
    assert result["source"] == "stats_fallback"


@pytest.mark.asyncio
async def test_qb_roster_only_no_stats():
    """Rookie QB on roster with zero weekly stats should return roster_only."""
    agent = TeamSystemsAgent(dry_run=False)

    seasonal = _mock_seasonal_roster([
        {"team": "NE", "position": "QB", "status": "ACT", "player_name": "Drake Maye"},
    ])
    # Weekly data has no Drake Maye rows at all
    weekly = _mock_weekly_qb_rows([
        {"player_name": "Mac Jones", "recent_team": "JAX", "attempts": 100, "completions": 60, "passing_yards": 800},
    ])

    from backend.utils.seasons import get_current_season
    current = get_current_season()
    agent._warehouse = _make_warehouse(
        seasonal_rosters=seasonal,
        qb_stats={current: weekly},
    )

    result = await agent._get_qb_data("NE", current)

    assert result["starter_name"] == "Drake Maye"
    assert result["source"] == "roster_only"
    assert "no stats found" in result["note"]


@pytest.mark.asyncio
async def test_qb_mismatch_logged(caplog):
    """When roster QB differs from stats leader, log the change."""
    import logging
    agent = TeamSystemsAgent(dry_run=False)

    seasonal = _mock_seasonal_roster([
        {"team": "SEA", "position": "QB", "status": "ACT", "player_name": "Sam Darnold"},
    ])
    weekly = _mock_weekly_qb_rows([
        {"player_name": "Geno Smith", "recent_team": "SEA", "attempts": 500, "completions": 320, "passing_yards": 3600},
        {"player_name": "Sam Darnold", "recent_team": "MIN", "attempts": 450, "completions": 280, "passing_yards": 3200},
    ])

    from backend.utils.seasons import get_current_season
    current = get_current_season()
    agent._warehouse = _make_warehouse(
        seasonal_rosters=seasonal,
        qb_stats={current: weekly},
    )

    with caplog.at_level(logging.INFO, logger="backend.agents.team_systems"):
        await agent._get_qb_data("SEA", current)

    assert any("QB CHANGE" in msg and "Sam Darnold" in msg and "Geno Smith" in msg for msg in caplog.messages)


@pytest.mark.asyncio
async def test_qb_lookup_uses_depth_chart_first():
    """Depth chart QB1 should be used before seasonal_rosters when available."""
    agent = TeamSystemsAgent(dry_run=False)
    from backend.utils.seasons import get_current_season
    current = get_current_season()

    # Depth chart says Josh Allen, seasonal roster says Kyle Allen (alphabetic first)
    seasonal = _mock_seasonal_roster([
        {"team": "BUF", "position": "QB", "status": "ACT", "player_name": "Kyle Allen"},
        {"team": "BUF", "position": "QB", "status": "ACT", "player_name": "Josh Allen"},
    ])
    qb_stats = _mock_weekly_qb_rows([
        {"player_name": "Josh Allen", "recent_team": "BUF",
         "attempts": 550, "completions": 370, "passing_yards": 4200},
        {"player_name": "Kyle Allen", "recent_team": "BUF",
         "attempts": 20, "completions": 10, "passing_yards": 150},
    ])

    agent._warehouse = _make_warehouse(
        seasonal_rosters=seasonal,
        qb_stats={current: qb_stats},
        starters={("BUF", "QB"): {"name": "Josh Allen", "gsis_id": "00-0034857", "depth_rank": 1}},
    )

    result = await agent._get_qb_data("BUF", current)
    assert "Josh Allen" in result.get("starter_name", "")
