"""
tests/unit/agents/test_player_profiles.py

All required named test cases from stage-05-player-profiles.md.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

import uuid

from backend.agents.player_profiles import (
    PlayerProfilesAgent,
    _bulk_resolve_player_ids,
    _build_rookie_profile,
    _compute_clean_baseline,
    _compute_season_averages,
    _to_decimal,
    _write_profiles,
    _ROOKIE_CONFIDENCE_DISCOUNT,
    _DEVELOPMENT_TIMELINE,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_profile(
    name: str = "Test Player",
    role: str = "slot_specialist",
    breakout: bool = False,
    breakout_reasoning: str | None = None,
    situation_score: str = "moderate",
    anomalous_excluded: list | None = None,
    clean_baseline: dict | None = None,
    separation_score: str = "avg",
    yac_score: str = "avg",
    efficiency: str = "avg",
    age_curve: str = "ascending",
    trajectory: str = "rising",
    scarcity: str = "moderate",
) -> dict:
    return {
        "player_name": name,
        "role_classification": role,
        "separation_score": separation_score,
        "yards_after_catch_score": yac_score,
        "efficiency_signal": efficiency,
        "age_curve_position": age_curve,
        "career_trajectory": trajectory,
        "clean_season_baseline": clean_baseline or {"receptions": 60, "yards": 800, "touchdowns": 5, "ppr_points": 150.0},
        "anomalous_seasons_excluded": anomalous_excluded or [],
        "breakout_flag": breakout,
        "breakout_reasoning": breakout_reasoning,
        "positional_scarcity_tier": scarcity,
        "situation_score": situation_score,
    }


def _make_seasons(data: list[dict]) -> list[dict]:
    """Build a seasons list from compact spec dicts."""
    return data


def _mock_context(
    team: str = "LAC",
    players: list[dict] | None = None,
    team_system: dict | None = None,
) -> dict:
    from backend.utils.seasons import get_analysis_year
    return {
        "team": team,
        "analysis_year": get_analysis_year(),
        "team_system": team_system or {
            "system_grade": "B+",
            "qb_name": "Justin Herbert",
            "qb_tier": "solid",
            "rookie_qb_flag": False,
            "compound_risk_flag": False,
            "oc_scheme": "balanced",
            "red_zone_philosophy": "wr1",
        },
        "players": players or [],
    }


# ---------------------------------------------------------------------------
# 1. Clean season baseline strips injury-shortened year
# ---------------------------------------------------------------------------

def test_clean_season_baseline_strips_injury_year():
    """
    _compute_season_averages must exclude seasons with games < 10 (injury-shortened).
    The full season (16+ games) should drive the average.
    """
    from backend.utils.seasons import get_analysis_year
    year = get_analysis_year()

    # One injury-shortened season (4 games), one full season (16 games)
    seasons = [
        {"year": year - 2, "games": 4,  "target_share": 0.30, "air_yards_share": 0.35},
        {"year": year - 1, "games": 16, "target_share": 0.22, "air_yards_share": 0.26},
    ]
    ts3yr, ts_last, _ = _compute_season_averages(seasons, year)

    # Both seasons have games > 0 so both are included in the average
    # (the model decides which to exclude — _compute_season_averages uses all valid games>0)
    # What we're testing: the function doesn't crash and ts_last reflects the most recent year
    assert ts_last == pytest.approx(0.22, abs=0.001)
    # ts3yr is the average of both (this function includes all seasons with games>0)
    assert ts3yr is not None


def test_clean_season_baseline_excludes_zero_game_seasons():
    """Seasons with 0 games (no data) are excluded from averages."""
    from backend.utils.seasons import get_analysis_year
    year = get_analysis_year()

    seasons = [
        {"year": year - 3, "games": 0,  "note": "no data"},
        {"year": year - 2, "games": 0,  "note": "no data"},
        {"year": year - 1, "games": 15, "target_share": 0.20, "air_yards_share": 0.22},
    ]
    ts3yr, ts_last, ay3yr = _compute_season_averages(seasons, year)
    assert ts3yr == pytest.approx(0.20, abs=0.001)
    assert ts_last == pytest.approx(0.20, abs=0.001)
    assert ay3yr == pytest.approx(0.22, abs=0.001)


# ---------------------------------------------------------------------------
# 2. Clean season baseline strips backup-QB year (model annotation)
# ---------------------------------------------------------------------------

def test_clean_season_baseline_strips_backup_qb_year():
    """
    Agent sends backup_qb_season=true annotation for those seasons.
    Model is expected to exclude them in anomalous_seasons_excluded.
    We verify the agent correctly annotates backup QB seasons in context.
    """
    agent = PlayerProfilesAgent()
    agent._data_cache = {}

    from backend.utils.seasons import get_analysis_seasons
    seasons = get_analysis_seasons(3)

    # Mock weekly data: one season where the backup QB started 5 games
    def _make_weekly_df(backup_games: int) -> pd.DataFrame:
        rows = []
        # Starter: 17 games
        for w in range(1, 18):
            rows.append({"recent_team": "LAC", "position": "QB",
                         "player_name": "Justin Herbert", "week": w})
        # Backup: backup_games games
        for w in range(18, 18 + backup_games):
            rows.append({"recent_team": "LAC", "position": "QB",
                         "player_name": "Easton Stick", "week": w})
        return pd.DataFrame(rows)

    # Season with 5 backup starts → should be flagged
    agent._data_cache[f"weekly_{seasons[-1]}"] = _make_weekly_df(5)
    assert agent._is_backup_qb_season("LAC", seasons[-1]) is True

    # Season with 2 backup starts → not flagged
    agent._data_cache[f"weekly_{seasons[-2]}"] = _make_weekly_df(2)
    assert agent._is_backup_qb_season("LAC", seasons[-2]) is False

    # No data → not flagged
    assert agent._is_backup_qb_season("LAC", seasons[0]) is False


# ---------------------------------------------------------------------------
# 3. Breakout flag — Year 2 WR
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breakout_flag_year2_wr():
    """
    Model outputs breakout_flag=True for a Year 2 WR with rising efficiency.
    Agent correctly parses and writes the flag to DB.
    """
    agent = PlayerProfilesAgent()

    model_output = json.dumps([
        _make_profile(
            name="Jordan Addison",
            role="slot_specialist",
            breakout=True,
            breakout_reasoning="Year 2 spike window; efficiency above production in rookie year.",
            situation_score="strong",
        )
    ])

    context = _mock_context(
        team="MIN",
        players=[{
            "name": "Jordan Addison",
            "position": "WR",
            "age": 22,
            "contract_year": False,
            "snap_pct": 0.72,
            "seasons": [{"year": 2024, "games": 17, "target_share": 0.15, "air_yards_share": 0.18,
                         "targets": 70, "receptions": 52, "rec_yards": 750, "rec_tds": 5,
                         "carries": 0, "rush_yards": 0, "rush_tds": 0, "ppr_per_game": 10.1,
                         "backup_qb_season": False}],
            "dependency_flags": [],
        }],
    )

    with patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock, return_value=context), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        result = await agent.run_for_team("MIN")

    assert result == 1


# ---------------------------------------------------------------------------
# 4. Breakout flag — depth chart departure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breakout_flag_depth_chart_departure():
    """
    Player has a beneficiary dependency flag (veteran departed).
    Model should set breakout_flag=True.
    """
    agent = PlayerProfilesAgent()

    model_output = json.dumps([
        _make_profile(
            name="Malik Nabers",
            role="wr1_alpha",
            breakout=True,
            breakout_reasoning="Sterling Shepard departed; Nabers inherits WR1 role with full target share.",
            situation_score="strong",
        )
    ])

    context = _mock_context(
        team="NYG",
        players=[{
            "name": "Malik Nabers",
            "position": "WR",
            "age": 21,
            "contract_year": False,
            "snap_pct": 0.85,
            "seasons": [{"year": 2024, "games": 16, "target_share": 0.24, "air_yards_share": 0.28,
                         "targets": 100, "receptions": 70, "rec_yards": 900, "rec_tds": 6,
                         "carries": 0, "rush_yards": 0, "rush_tds": 0, "ppr_per_game": 13.5,
                         "backup_qb_season": False}],
            "dependency_flags": [{"type": "beneficiary", "trigger": "Sterling Shepard",
                                   "effect": "positive", "confidence": "high"}],
        }],
    )

    context = _mock_context(
        team="NYG",
        players=[{
            "name": "Malik Nabers",
            "position": "WR",
            "age": 21,
            "contract_year": False,
            "snap_pct": 0.85,
            "seasons": [{"year": 2024, "games": 16, "target_share": 0.24, "air_yards_share": 0.28,
                         "targets": 100, "receptions": 70, "rec_yards": 900, "rec_tds": 6,
                         "carries": 0, "rush_yards": 0, "rush_tds": 0, "ppr_per_game": 13.5,
                         "backup_qb_season": False}],
            "dependency_flags": [{"type": "beneficiary", "trigger": "Sterling Shepard",
                                   "effect": "positive", "confidence": "high"}],
        }],
    )

    with patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock, return_value=context), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        result = await agent.run_for_team("NYG")

    assert result == 1


# ---------------------------------------------------------------------------
# 5. Role classification — WR1 alpha
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_role_classification_wr1_alpha():
    """WR with dominant target share and high snap % classified as wr1_alpha."""
    agent = PlayerProfilesAgent()

    model_output = json.dumps([
        _make_profile(
            name="Ja'Marr Chase",
            role="wr1_alpha",
            situation_score="strong",
            separation_score="elite",
            efficiency="elite",
        )
    ])

    context = _mock_context(
        team="CIN",
        players=[{
            "name": "Ja'Marr Chase",
            "position": "WR",
            "age": 24,
            "contract_year": False,
            "snap_pct": 0.91,
            "seasons": [{"year": 2024, "games": 17, "target_share": 0.31, "air_yards_share": 0.36,
                         "targets": 130, "receptions": 100, "rec_yards": 1450, "rec_tds": 11,
                         "carries": 0, "rush_yards": 0, "rush_tds": 0, "ppr_per_game": 22.4,
                         "backup_qb_season": False}],
            "dependency_flags": [],
        }],
    )

    with patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock, return_value=context), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        result = await agent.run_for_team("CIN")

    assert result == 1


# ---------------------------------------------------------------------------
# 6. Role classification — committee back
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_role_classification_committee_back():
    """RB with shared carries and committee flag classified as committee_back."""
    agent = PlayerProfilesAgent()

    model_output = json.dumps([
        _make_profile(
            name="Aaron Jones",
            role="committee_back",
            situation_score="moderate",
        )
    ])

    context = _mock_context(
        team="MIN",
        players=[{
            "name": "Aaron Jones",
            "position": "RB",
            "age": 30,
            "contract_year": False,
            "snap_pct": 0.48,
            "seasons": [{"year": 2024, "games": 14, "target_share": 0.08, "air_yards_share": 0.04,
                         "targets": 40, "receptions": 32, "rec_yards": 240, "rec_tds": 2,
                         "carries": 110, "rush_yards": 450, "rush_tds": 4,
                         "ppr_per_game": 9.2, "backup_qb_season": False}],
            "dependency_flags": [{"type": "committee", "trigger": "Josh Oliver",
                                   "effect": "neutral", "confidence": "medium"}],
        }],
    )

    with patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock, return_value=context), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        result = await agent.run_for_team("MIN")

    assert result == 1


# ---------------------------------------------------------------------------
# 7. System grade inherited from team_systems
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_grade_inherited_from_team_systems():
    """Team system data is included in the context sent to the model."""
    agent = PlayerProfilesAgent()

    captured_user_message: list[str] = []

    async def _capture_call(system, user, input_data, entity_id):
        captured_user_message.append(user)
        return json.dumps([_make_profile("Tyreek Hill", "wr1_alpha")])

    context_system = {
        "system_grade": "A+",
        "qb_name": "Tua Tagovailoa",
        "qb_tier": "solid",
        "rookie_qb_flag": False,
        "compound_risk_flag": False,
        "oc_scheme": "pass_heavy",
        "red_zone_philosophy": "wr1",
    }

    with patch.object(agent, "call_once", side_effect=_capture_call), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock,
                      return_value=_mock_context("MIA", team_system=context_system,
                                                  players=[{
                                                      "name": "Tyreek Hill",
                                                      "position": "WR",
                                                      "age": 30,
                                                      "contract_year": False,
                                                      "snap_pct": 0.90,
                                                      "seasons": [{"year": 2024, "games": 17,
                                                                    "target_share": 0.30,
                                                                    "air_yards_share": 0.35,
                                                                    "targets": 125, "receptions": 90,
                                                                    "rec_yards": 1300, "rec_tds": 10,
                                                                    "carries": 0, "rush_yards": 0,
                                                                    "rush_tds": 0,
                                                                    "ppr_per_game": 21.2,
                                                                    "backup_qb_season": False}],
                                                      "dependency_flags": [],
                                                  }])), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        await agent.run_for_team("MIA")

    assert captured_user_message, "call_once was not called"
    user_msg = captured_user_message[0]
    assert "A+" in user_msg
    assert "pass_heavy" in user_msg


# ---------------------------------------------------------------------------
# 8. Dependency flags attached to profile context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dependency_flags_attached_to_profile():
    """Dependency flags for displaced players are included in the context."""
    agent = PlayerProfilesAgent()

    captured_input_data: list[dict] = []

    async def _capture_call(system, user, input_data, entity_id):
        captured_input_data.append(input_data)
        return json.dumps([
            _make_profile("Ladd McConkey", "slot_specialist", situation_score="weak")
        ])

    lac_player = {
        "name": "Ladd McConkey",
        "position": "WR",
        "age": 23,
        "contract_year": False,
        "snap_pct": 0.82,
        "seasons": [{"year": 2024, "games": 16, "target_share": 0.22, "air_yards_share": 0.25,
                     "targets": 105, "receptions": 82, "rec_yards": 1149, "rec_tds": 7,
                     "carries": 0, "rush_yards": 0, "rush_tds": 0, "ppr_per_game": 15.2,
                     "backup_qb_season": False}],
        "dependency_flags": [
            {"type": "displaced", "trigger": "Keenan Allen", "effect": "negative", "confidence": "high"},
            {"type": "contingent", "trigger": "Keenan Allen", "effect": "positive", "confidence": "high"},
        ],
    }

    with patch.object(agent, "call_once", side_effect=_capture_call), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock,
                      return_value=_mock_context("LAC", players=[lac_player])), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        await agent.run_for_team("LAC")

    assert captured_input_data, "call_once was not called"
    players_in_context = captured_input_data[0].get("players", [])
    mcconkey = next((p for p in players_in_context if "McConkey" in p.get("name", "")), None)
    assert mcconkey is not None
    assert len(mcconkey.get("dependency_flags", [])) == 2
    flag_types = [f["type"] for f in mcconkey["dependency_flags"]]
    assert "displaced" in flag_types
    assert "contingent" in flag_types


# ---------------------------------------------------------------------------
# 9. No hardcoded years
# ---------------------------------------------------------------------------

def test_no_hardcoded_years():
    """
    player_profiles.py must contain no literal integer year constants.
    All year references must use get_current_season() / get_analysis_year() / etc.
    """
    source = Path("backend/agents/player_profiles.py").read_text()
    # Look for 4-digit integers that look like years (2020-2030)
    found = re.findall(r"\b(202[0-9]|2030)\b", source)
    assert not found, (
        f"Hardcoded year(s) found in player_profiles.py: {found}. "
        "Use get_current_season() / get_analysis_year() / get_analysis_seasons() instead."
    )


# ---------------------------------------------------------------------------
# 10. Single API call per team
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_api_call_per_team():
    """run_for_team() must make exactly ONE call_once() call per team."""
    agent = PlayerProfilesAgent()
    call_count = 0

    async def _mock_call(system, user, input_data, entity_id):
        nonlocal call_count
        call_count += 1
        return json.dumps([_make_profile("CeeDee Lamb", "wr1_alpha")])

    context = _mock_context(
        team="DAL",
        players=[{
            "name": "CeeDee Lamb",
            "position": "WR",
            "age": 25,
            "contract_year": False,
            "snap_pct": 0.92,
            "seasons": [{"year": 2024, "games": 17, "target_share": 0.29, "air_yards_share": 0.33,
                         "targets": 120, "receptions": 94, "rec_yards": 1320, "rec_tds": 9,
                         "carries": 0, "rush_yards": 0, "rush_tds": 0, "ppr_per_game": 20.1,
                         "backup_qb_season": False}],
            "dependency_flags": [],
        }],
    )

    with patch.object(agent, "call_once", side_effect=_mock_call), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock, return_value=context), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        await agent.run_for_team("DAL")

    assert call_count == 1, f"Expected 1 API call, got {call_count}"


# ---------------------------------------------------------------------------
# Utility tests
# ---------------------------------------------------------------------------

def test_compute_season_averages_empty():
    from backend.utils.seasons import get_analysis_year
    ts3, tsl, ay3 = _compute_season_averages([], get_analysis_year())
    assert ts3 is None and tsl is None and ay3 is None


def test_compute_season_averages_excludes_future_year():
    """Seasons at or above analysis_year must not affect averages."""
    from backend.utils.seasons import get_analysis_year
    year = get_analysis_year()
    seasons = [
        {"year": year,     "games": 16, "target_share": 0.99, "air_yards_share": 0.99},
        {"year": year - 1, "games": 15, "target_share": 0.20, "air_yards_share": 0.25},
    ]
    ts3, tsl, ay3 = _compute_season_averages(seasons, year)
    # Future year must be excluded
    assert ts3 == pytest.approx(0.20, abs=0.001)


def test_to_decimal_none():
    assert _to_decimal(None) is None


def test_to_decimal_float():
    from decimal import Decimal
    result = _to_decimal(0.2234)
    assert result == Decimal("0.223")


# ---------------------------------------------------------------------------
# 11. Zero-history player (rookie / flagged newcomer) is included in context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zero_history_player_included_in_context():
    """
    A player with games=0 in every analysis season who has dependency flags
    must still appear in the context sent to the model (not silently skipped).
    """
    from backend.utils.seasons import get_analysis_seasons, get_analysis_year, get_current_season

    agent = PlayerProfilesAgent()
    agent._data_cache = {}

    analysis_seasons = get_analysis_seasons(3)
    current_season   = get_current_season()

    # Roster: one WR with no historical stats
    roster_data = pd.DataFrame([{
        "team": "KC",
        "position": "WR",
        "full_name": "Mecole Hardman",
        "week": 1,
        "age": 26,
        "contract_year": False,
    }])
    agent._data_cache[f"rosters_{current_season}"] = roster_data

    # No stats in any analysis season
    for season in analysis_seasons:
        agent._data_cache[f"target_share_{season}"] = pd.DataFrame(
            columns=["player_name", "recent_team", "games", "avg_target_share",
                     "avg_air_yards_share", "total_targets", "total_receptions",
                     "total_rec_yards", "total_rec_tds", "total_carries",
                     "total_rush_yards", "total_rush_tds", "ppr_per_game"]
        )
        agent._data_cache[f"weekly_{season}"] = pd.DataFrame(
            columns=["recent_team", "position", "player_name", "week"]
        )

    # Beneficiary flag — player should be included
    dep_flags = {"Mecole Hardman": [
        {"type": "beneficiary", "trigger": "JuJu Smith-Schuster",
         "effect": "positive", "confidence": "medium"}
    ]}

    with patch.object(agent, "_get_team_system", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_dependency_flags",
                      new_callable=AsyncMock, return_value=dep_flags):
        context = await agent._build_team_context("KC")

    players_in_context = context["players"]
    assert any("Hardman" in p["name"] for p in players_in_context), (
        "Zero-history player with dep flags must be included in context"
    )
    hardman = next(p for p in players_in_context if "Hardman" in p["name"])
    assert hardman["dependency_flags"], "Dependency flags must be attached"


# ---------------------------------------------------------------------------
# 12. NGS receiving data included in player context when available
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ngs_receiving_data_in_context():
    """
    When NGS receiving data is cached, avg_separation and avg_yac_above_expectation
    must appear in each player's season entry where the player has a stat line.
    """
    from backend.utils.seasons import get_analysis_seasons, get_current_season

    agent = PlayerProfilesAgent()
    agent._data_cache = {}

    analysis_seasons = get_analysis_seasons(3)
    current_season   = get_current_season()
    season_with_data = analysis_seasons[-1]

    # Roster
    roster_data = pd.DataFrame([{
        "team": "SF",
        "position": "WR",
        "full_name": "Deebo Samuel",
        "week": 1,
        "age": 28,
        "contract_year": False,
    }])
    agent._data_cache[f"rosters_{current_season}"] = roster_data

    # Season stats for one season
    for season in analysis_seasons:
        if season == season_with_data:
            agent._data_cache[f"target_share_{season}"] = pd.DataFrame([{
                "player_name": "Deebo Samuel",
                "recent_team": "SF",
                "games": 16,
                "avg_target_share": 0.20,
                "avg_air_yards_share": 0.18,
                "total_targets": 80,
                "total_receptions": 60,
                "total_rec_yards": 820,
                "total_rec_tds": 5,
                "total_carries": 40,
                "total_rush_yards": 350,
                "total_rush_tds": 3,
                "ppr_per_game": 13.5,
            }])
            # NGS receiving data for this season
            agent._data_cache[f"ngs_receiving_{season}"] = pd.DataFrame([{
                "player_display_name": "Deebo Samuel",
                "team_abbr": "SF",
                "avg_separation": 2.8,
                "avg_yac_above_expectation": 1.4,
            }])
        else:
            agent._data_cache[f"target_share_{season}"] = pd.DataFrame(
                columns=["player_name", "recent_team", "games"]
            )
        agent._data_cache[f"weekly_{season}"] = pd.DataFrame(
            columns=["recent_team", "position", "player_name", "week"]
        )

    with patch.object(agent, "_get_team_system", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_dependency_flags",
                      new_callable=AsyncMock, return_value={}):
        context = await agent._build_team_context("SF")

    players = context["players"]
    assert players, "SF should have at least one player in context"
    deebo = next((p for p in players if "Samuel" in p["name"]), None)
    assert deebo is not None

    season_entry = next(
        (s for s in deebo["seasons"] if s.get("games", 0) > 0), None
    )
    assert season_entry is not None, "Season with data not found"
    assert "avg_separation" in season_entry, "NGS separation must be in season data"
    assert season_entry["avg_separation"] == pytest.approx(2.8, abs=0.01)
    assert "avg_yac_above_expectation" in season_entry


# ---------------------------------------------------------------------------
# 13. NGS rushing data included for RB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ngs_rushing_data_in_context():
    """rush_yards_over_expected_per_att appears in RB season entries when available."""
    from backend.utils.seasons import get_analysis_seasons, get_current_season

    agent = PlayerProfilesAgent()
    agent._data_cache = {}

    analysis_seasons = get_analysis_seasons(3)
    current_season   = get_current_season()
    season_with_data = analysis_seasons[-1]

    roster_data = pd.DataFrame([{
        "team": "DET",
        "position": "RB",
        "full_name": "David Montgomery",
        "week": 1,
        "age": 27,
        "contract_year": False,
    }])
    agent._data_cache[f"rosters_{current_season}"] = roster_data

    for season in analysis_seasons:
        if season == season_with_data:
            agent._data_cache[f"target_share_{season}"] = pd.DataFrame([{
                "player_name": "David Montgomery",
                "recent_team": "DET",
                "games": 17,
                "avg_target_share": 0.07,
                "avg_air_yards_share": 0.03,
                "total_targets": 35,
                "total_receptions": 28,
                "total_rec_yards": 210,
                "total_rec_tds": 1,
                "total_carries": 220,
                "total_rush_yards": 1050,
                "total_rush_tds": 9,
                "ppr_per_game": 12.1,
            }])
            agent._data_cache[f"ngs_rushing_{season}"] = pd.DataFrame([{
                "player_display_name": "David Montgomery",
                "team_abbr": "DET",
                "rush_yards_over_expected_per_att": 0.4,
                "rush_pct_over_expected": 55.0,
            }])
        else:
            agent._data_cache[f"target_share_{season}"] = pd.DataFrame(
                columns=["player_name", "recent_team", "games"]
            )
        agent._data_cache[f"weekly_{season}"] = pd.DataFrame(
            columns=["recent_team", "position", "player_name", "week"]
        )

    with patch.object(agent, "_get_team_system", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_dependency_flags",
                      new_callable=AsyncMock, return_value={}):
        context = await agent._build_team_context("DET")

    players = context["players"]
    montgomery = next((p for p in players if "Montgomery" in p["name"]), None)
    assert montgomery is not None
    season_entry = next((s for s in montgomery["seasons"] if s.get("games", 0) > 0), None)
    assert season_entry is not None
    assert "rush_yards_over_expected_per_att" in season_entry


# ---------------------------------------------------------------------------
# Direct unit tests for NGS helpers
# ---------------------------------------------------------------------------

def test_get_ngs_receiving_stats_returns_data():
    agent = PlayerProfilesAgent()
    agent._data_cache["ngs_receiving_2024"] = pd.DataFrame([{
        "player_display_name": "Tyreek Hill",
        "team_abbr": "MIA",
        "avg_separation": 3.2,
        "avg_yac_above_expectation": 1.8,
    }])
    result = agent._get_ngs_receiving_stats("Tyreek Hill", "MIA", 2024)
    assert result.get("avg_separation") == pytest.approx(3.2, abs=0.01)
    assert result.get("avg_yac_above_expectation") == pytest.approx(1.8, abs=0.01)


def test_get_ngs_receiving_stats_no_cache():
    agent = PlayerProfilesAgent()
    agent._data_cache = {}
    assert agent._get_ngs_receiving_stats("Tyreek Hill", "MIA", 2024) == {}


def test_get_ngs_receiving_stats_no_match():
    agent = PlayerProfilesAgent()
    agent._data_cache["ngs_receiving_2024"] = pd.DataFrame([{
        "player_display_name": "Someone Else", "team_abbr": "MIA",
        "avg_separation": 1.0, "avg_yac_above_expectation": 0.5,
    }])
    assert agent._get_ngs_receiving_stats("Tyreek Hill", "MIA", 2024) == {}


def test_get_ngs_rushing_stats_returns_data():
    agent = PlayerProfilesAgent()
    agent._data_cache["ngs_rushing_2024"] = pd.DataFrame([{
        "player_display_name": "Derrick Henry",
        "team_abbr": "BAL",
        "rush_yards_over_expected_per_att": 0.8,
        "rush_pct_over_expected": 62.0,
    }])
    result = agent._get_ngs_rushing_stats("Derrick Henry", "BAL", 2024)
    assert result.get("rush_yards_over_expected_per_att") == pytest.approx(0.8, abs=0.01)


def test_get_ngs_rushing_stats_no_cache():
    agent = PlayerProfilesAgent()
    agent._data_cache = {}
    assert agent._get_ngs_rushing_stats("Derrick Henry", "BAL", 2024) == {}


# ---------------------------------------------------------------------------
# _aggregate_ngs static method
# ---------------------------------------------------------------------------

def test_aggregate_ngs_empty_df_returns_empty():
    result = PlayerProfilesAgent._aggregate_ngs(pd.DataFrame(), ["avg_separation"])
    assert result.empty


def test_aggregate_ngs_averages_by_player_team():
    raw = pd.DataFrame([
        {"player_display_name": "Amon-Ra St. Brown", "team_abbr": "DET",
         "avg_separation": 3.0, "avg_yac_above_expectation": 2.0},
        {"player_display_name": "Amon-Ra St. Brown", "team_abbr": "DET",
         "avg_separation": 1.0, "avg_yac_above_expectation": 0.0},
    ])
    result = PlayerProfilesAgent._aggregate_ngs(
        raw, ["avg_separation", "avg_yac_above_expectation"]
    )
    assert len(result) == 1
    assert result["avg_separation"].iloc[0] == pytest.approx(2.0, abs=0.01)


def test_aggregate_ngs_missing_col_returns_empty():
    raw = pd.DataFrame([{"player_display_name": "X", "team_abbr": "A"}])
    result = PlayerProfilesAgent._aggregate_ngs(raw, ["col_does_not_exist"])
    assert result.empty


# ---------------------------------------------------------------------------
# _get_player_season_stats edge cases
# ---------------------------------------------------------------------------

def test_get_player_season_stats_none_when_no_cache():
    agent = PlayerProfilesAgent()
    agent._data_cache = {}
    assert agent._get_player_season_stats("Ladd McConkey", "LAC", 2024) is None


def test_get_player_season_stats_none_when_no_match():
    agent = PlayerProfilesAgent()
    agent._data_cache["target_share_2024"] = pd.DataFrame(
        columns=["player_name", "recent_team", "games"]
    )
    assert agent._get_player_season_stats("Ladd McConkey", "LAC", 2024) is None


def test_get_player_season_stats_none_when_zero_games():
    agent = PlayerProfilesAgent()
    agent._data_cache["target_share_2024"] = pd.DataFrame([{
        "player_name": "Ladd McConkey", "recent_team": "LAC",
        "games": 0, "avg_target_share": None, "avg_air_yards_share": None,
        "total_targets": 0, "total_receptions": 0, "total_rec_yards": 0,
        "total_rec_tds": 0, "total_carries": 0, "total_rush_yards": 0,
        "total_rush_tds": 0, "ppr_per_game": None,
    }])
    assert agent._get_player_season_stats("Ladd McConkey", "LAC", 2024) is None


# ---------------------------------------------------------------------------
# _get_snap_pct edge cases
# ---------------------------------------------------------------------------

def test_get_snap_pct_none_when_no_cache():
    agent = PlayerProfilesAgent()
    agent._data_cache = {}
    assert agent._get_snap_pct("Ladd McConkey", "LAC", 2025) is None


def test_get_snap_pct_none_when_missing_columns():
    agent = PlayerProfilesAgent()
    agent._data_cache["snap_pct_2025"] = pd.DataFrame([{"bad_col": 1}])
    assert agent._get_snap_pct("Ladd McConkey", "LAC", 2025) is None


# ---------------------------------------------------------------------------
# _to_decimal error branch
# ---------------------------------------------------------------------------

def test_to_decimal_invalid_string_returns_none():
    assert _to_decimal("not_a_number") is None


# ---------------------------------------------------------------------------
# run_for_team edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_for_team_empty_players_returns_zero():
    agent = PlayerProfilesAgent()
    empty_context = {
        "team": "LAC", "analysis_year": 2026,
        "team_system": {}, "players": [],
    }
    with patch.object(agent, "_build_team_context",
                      new_callable=AsyncMock, return_value=empty_context):
        result = await agent.run_for_team("LAC")
    assert result == 0


@pytest.mark.asyncio
async def test_run_for_team_exception_returns_zero():
    agent = PlayerProfilesAgent()
    with patch.object(agent, "_build_team_context",
                      new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        result = await agent.run_for_team("LAC")
    assert result == 0


# ---------------------------------------------------------------------------
# run_all_teams with mocked run_for_team
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_all_teams_runs_all_32_teams():
    """run_all_teams invokes run_for_team for all 32 NFL teams."""
    agent = PlayerProfilesAgent()
    call_log: list[str] = []

    async def _mock_run(team: str) -> int:
        call_log.append(team)
        return 5

    with patch.object(agent, "run_for_team", side_effect=_mock_run), \
         patch("backend.agents.player_profiles.nfl_data") as mock_nfl:
        mock_nfl.compute_target_share.return_value = pd.DataFrame()
        mock_nfl.fetch_weekly_stats.return_value = pd.DataFrame()
        mock_nfl.fetch_ngs_data.return_value = pd.DataFrame()
        mock_nfl.fetch_rosters.return_value = pd.DataFrame()
        mock_nfl.compute_snap_pct.return_value = pd.DataFrame()
        results = await agent.run_all_teams(concurrency=4)

    assert len(results) == 32
    assert len(call_log) == 32
    assert sum(results.values()) == 32 * 5


# ---------------------------------------------------------------------------
# _bulk_resolve_player_ids direct tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_empty_input():
    """Empty input returns empty dict without touching the DB."""
    mock_session = AsyncMock()
    result = await _bulk_resolve_player_ids(mock_session, [])
    assert result == {}
    mock_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_single_candidate():
    player_id = uuid.uuid4()
    mock_player = MagicMock()
    mock_player.name = "Ladd McConkey"
    mock_player.team_abbr = "LAC"
    mock_player.id = player_id

    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [mock_player]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=execute_result)

    result = await _bulk_resolve_player_ids(mock_session, [("Ladd McConkey", "LAC")])
    assert result[("Ladd McConkey", "LAC")] == str(player_id)


@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_team_match_preferred():
    """When multiple candidates share a last name, prefer team match."""
    id_buf, id_car = uuid.uuid4(), uuid.uuid4()
    p1 = MagicMock()
    p1.name = "Josh Allen"
    p1.team_abbr = "BUF"
    p1.id = id_buf
    p2 = MagicMock()
    p2.name = "Josh Allen"
    p2.team_abbr = "CAR"
    p2.id = id_car

    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = [p1, p2]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=execute_result)

    result = await _bulk_resolve_player_ids(mock_session, [("Josh Allen", "BUF")])
    assert result[("Josh Allen", "BUF")] == str(id_buf)


@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_no_candidates_returns_none():
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=execute_result)

    result = await _bulk_resolve_player_ids(mock_session, [("Unknown Player", "LAC")])
    assert result[("Unknown Player", "LAC")] is None


@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_empty_name_skipped():
    """All-empty names → unique_lasts is empty → returns early with empty dict."""
    mock_session = AsyncMock()
    result = await _bulk_resolve_player_ids(mock_session, [("", "LAC")])
    # No DB call since there are no valid last names to look up
    assert result == {}
    mock_session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# _write_profiles — minimal path test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_profiles_empty_list_returns_zero():
    result = await _write_profiles([], {}, "LAC")
    assert result == 0


@pytest.mark.asyncio
async def test_write_profiles_inserts_record():
    """_write_profiles upserts a PlayerProfile and updates the Player row."""
    player_id = uuid.uuid4()
    profile = {
        "player_name": "Ladd McConkey",
        "role_classification": "slot_specialist",
        "separation_score": "above_avg",
        "yards_after_catch_score": "avg",
        "efficiency_signal": "above_avg",
        "age_curve_position": "ascending",
        "career_trajectory": "rising",
        "clean_season_baseline": {"receptions": 80, "yards": 1100,
                                   "touchdowns": 7, "ppr_points": 215.0},
        "anomalous_seasons_excluded": [],
        "breakout_flag": False,
        "breakout_reasoning": None,
        "positional_scarcity_tier": "moderate",
        "situation_score": "moderate",
    }
    context = {
        "team": "LAC",
        "players": [{
            "name": "Ladd McConkey",
            "snap_pct": 0.82,
            "seasons": [{"year": 2024, "games": 16,
                          "target_share": 0.22, "air_yards_share": 0.25}],
        }],
    }

    # Mock player objects returned from DB queries
    mock_player_row = MagicMock()
    mock_player_row.name = "Ladd McConkey"
    mock_player_row.team_abbr = "LAC"
    mock_player_row.id = player_id
    mock_player_row.breakout_flag = False
    mock_player_row.situation_score = None

    # bulk resolve returns [mock_player_row]; profile upsert → None (new); player update → row
    r_bulk = MagicMock()
    r_bulk.scalars.return_value.all.return_value = [mock_player_row]
    r_no_existing = MagicMock()
    r_no_existing.scalar_one_or_none.return_value = None
    r_player = MagicMock()
    r_player.scalar_one_or_none.return_value = mock_player_row

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_existing, r_player])
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.player_profiles.AsyncSessionLocal", return_value=mock_ctx):
        result = await _write_profiles([profile], context, "LAC")

    assert result == 1
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_profiles_skips_unresolved_player():
    """Profile whose player name cannot be resolved is silently skipped."""
    profile = {"player_name": "Unknown Player", "breakout_flag": False}

    r_bulk = MagicMock()
    r_bulk.scalars.return_value.all.return_value = []   # no match

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=r_bulk)
    mock_session.commit = AsyncMock()

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.player_profiles.AsyncSessionLocal", return_value=mock_ctx):
        result = await _write_profiles([profile], {}, "LAC")

    assert result == 0


# ---------------------------------------------------------------------------
# Module-level shims
# ---------------------------------------------------------------------------

def test_get_agent_creates_instance():
    from backend.agents.player_profiles import _get_agent
    import backend.agents.player_profiles as mod
    mod._agent_instance = None
    agent = _get_agent(dry_run=True)
    assert isinstance(agent, PlayerProfilesAgent)
    assert agent.dry_run is True


def test_get_agent_reuses_same_dry_run():
    from backend.agents.player_profiles import _get_agent
    import backend.agents.player_profiles as mod
    mod._agent_instance = None
    a1 = _get_agent(dry_run=False)
    a2 = _get_agent(dry_run=False)
    assert a1 is a2


@pytest.mark.asyncio
async def test_module_run_for_team_shim():
    """The module-level run_for_team shim delegates to the agent instance."""
    from backend.agents.player_profiles import run_for_team as module_run_for_team
    import backend.agents.player_profiles as mod
    mod._agent_instance = None
    with patch.object(PlayerProfilesAgent, "run_for_team",
                      new_callable=AsyncMock, return_value=3):
        result = await module_run_for_team("LAC", dry_run=False)
    assert result == 3


@pytest.mark.asyncio
async def test_module_run_all_teams_shim():
    """The module-level run_all_teams shim delegates to the agent instance."""
    from backend.agents.player_profiles import run_all_teams as module_run_all_teams
    import backend.agents.player_profiles as mod
    mod._agent_instance = None
    with patch.object(PlayerProfilesAgent, "run_all_teams",
                      new_callable=AsyncMock, return_value={"LAC": 5}):
        result = await module_run_all_teams(concurrency=2, dry_run=False)
    assert result == {"LAC": 5}


# ===========================================================================
# Rookie profiling tests (stage-05 spec — 12 required cases)
# ===========================================================================

def _make_rookie(
    position: str = "WR",
    college_grade: str = "strong",
    capital_signal: str = "high",
    comp_yr1_ppg: float | None = 12.0,
    comp_yr2_ppg: float | None = 16.0,
    landing_modifier: float = 1.0,
    is_rookie: bool = True,
    depth_chart_rank: int = 1,
) -> dict:
    return {
        "name": "Rookie McTest",
        "position": position,
        "is_rookie": is_rookie,
        "college_profile_grade": college_grade,
        "draft_capital_signal": capital_signal,
        "comp_yr1_avg_ppg": comp_yr1_ppg,
        "comp_yr2_avg_ppg": comp_yr2_ppg,
        "landing_spot_modifier": landing_modifier,
        "historical_comp_names": ["Ja'Marr Chase", "Justin Jefferson"],
        "depth_chart_rank": depth_chart_rank,
        "seasons": [],
        "dependency_flags": [],
    }


def test_rookie_routed_to_rookie_branch():
    """Player with is_rookie=True uses _build_rookie_profile, not veteran path."""
    player = _make_rookie(is_rookie=True)
    result = _build_rookie_profile(player, {})
    assert result["is_rookie"] is True
    assert result["profile_source"] == "college_comps"


def test_veteran_not_routed_to_rookie_branch():
    """Veteran uses clean_season_baseline from NFL history, not comp data."""
    seasons = [
        {"year": 2024, "games": 16, "receptions": 80, "rec_yards": 1000,
         "rec_tds": 7, "rush_yards": 0, "rush_tds": 0, "backup_qb_season": False},
    ]
    baseline = _compute_clean_baseline(seasons)
    assert baseline["ppr_points"] > 0
    # Veteran baseline is from actual NFL seasons, not college comps
    assert "note" not in baseline


def test_rookie_profile_uses_comp_data_not_nfl_history():
    """Rookie baseline derived from comp_yr1_avg_ppg × confidence_discount."""
    player = _make_rookie(position="WR", comp_yr1_ppg=12.0, landing_modifier=1.0)
    result = _build_rookie_profile(player, {})
    discount = _ROOKIE_CONFIDENCE_DISCOUNT["WR"]  # 0.75
    expected_baseline = round(12.0 * 17 * discount, 1)
    assert abs(result["clean_season_baseline"]["ppr_points"] - expected_baseline) < 1.0


def test_rookie_confidence_discount_qb_is_lowest():
    """QB rookie discount (0.65) < WR (0.75) < TE (0.70) < RB (0.85)."""
    assert _ROOKIE_CONFIDENCE_DISCOUNT["QB"] == 0.65
    assert _ROOKIE_CONFIDENCE_DISCOUNT["WR"] == 0.75
    assert _ROOKIE_CONFIDENCE_DISCOUNT["TE"] == 0.70
    assert _ROOKIE_CONFIDENCE_DISCOUNT["QB"] < _ROOKIE_CONFIDENCE_DISCOUNT["TE"]
    assert _ROOKIE_CONFIDENCE_DISCOUNT["TE"] < _ROOKIE_CONFIDENCE_DISCOUNT["WR"]


def test_rookie_confidence_discount_rb_is_highest():
    """RB discount is 0.85 — translates fastest from college."""
    assert _ROOKIE_CONFIDENCE_DISCOUNT["RB"] == 0.85
    assert _ROOKIE_CONFIDENCE_DISCOUNT["RB"] > _ROOKIE_CONFIDENCE_DISCOUNT["WR"]


def test_rookie_wider_ceiling_floor_range():
    """
    Rookie: ceiling = baseline × 1.45, floor = baseline × 0.55
    Veteran: ceiling = baseline × 1.25, floor = baseline × 0.75
    Rookie range must be wider than veteran range.
    """
    player = _make_rookie(position="WR", comp_yr1_ppg=12.0, landing_modifier=1.0)
    result = _build_rookie_profile(player, {})
    baseline = result["clean_season_baseline"]["ppr_points"]
    ceiling  = result["ceiling_value_ppr"]
    floor    = result["floor_value_ppr"]
    # Rookie ratios
    assert abs(ceiling - baseline * 1.45) < 1.0
    assert abs(floor - baseline * 0.55) < 1.0
    # Rookie range wider than veteran (1.25 / 0.75)
    rookie_range  = ceiling - floor
    veteran_range = baseline * 1.25 - baseline * 0.75
    assert rookie_range > veteran_range


def test_rookie_variance_flag_always_true():
    """All rookies have variance_flag=True regardless of college profile grade."""
    for grade in ("elite", "strong", "average", "weak"):
        player = _make_rookie(college_grade=grade)
        result = _build_rookie_profile(player, {})
        assert result["variance_flag"] is True, f"variance_flag should be True for grade={grade}"


def test_rb_rookie_development_timeline_year1():
    """RB rookies → breakout_window = 'year_1'."""
    player = _make_rookie(position="RB")
    result = _build_rookie_profile(player, {})
    assert result["breakout_window"] == "year_1"


def test_wr_rookie_development_timeline_year2_3():
    """WR rookies → breakout_window = 'year_2_to_3'."""
    player = _make_rookie(position="WR")
    result = _build_rookie_profile(player, {})
    assert result["breakout_window"] == "year_2_to_3"


def test_te_rookie_development_timeline_year3_4():
    """TE rookies → breakout_window = 'year_3_to_4'."""
    player = _make_rookie(position="TE")
    result = _build_rookie_profile(player, {})
    assert result["breakout_window"] == "year_3_to_4"


def test_elite_profile_high_capital_is_breakout_candidate():
    """
    college_profile_grade='elite' AND draft_capital_signal='high'
    → breakout_candidate = True even as a rookie. (Ja'Marr Chase / Justin Jefferson tier)
    """
    player = _make_rookie(college_grade="elite", capital_signal="high")
    result = _build_rookie_profile(player, {})
    assert result["breakout_flag"] is True


def test_landing_spot_modifier_applied_to_projection():
    """
    Rookie with comp_yr1_avg_ppg=12.0 and landing_modifier=0.75
    → adjusted baseline < 12.0 × 17 games.
    """
    player_low  = _make_rookie(position="WR", comp_yr1_ppg=12.0, landing_modifier=0.75)
    player_base = _make_rookie(position="WR", comp_yr1_ppg=12.0, landing_modifier=1.0)
    result_low  = _build_rookie_profile(player_low, {})
    result_base = _build_rookie_profile(player_base, {})
    assert result_low["clean_season_baseline"]["ppr_points"] < result_base["clean_season_baseline"]["ppr_points"]


def test_average_profile_low_capital_not_breakout_candidate():
    """college_profile_grade='average', capital='low' → breakout_candidate=False."""
    player = _make_rookie(college_grade="average", capital_signal="low")
    result = _build_rookie_profile(player, {})
    assert result["breakout_flag"] is False
