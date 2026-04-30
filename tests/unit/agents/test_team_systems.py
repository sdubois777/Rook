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

from backend.agents.team_systems import TeamSystemsAgent, NFL_TEAMS


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
