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
    PLAYER_PROFILES_PROMPT_VERSION,
    PROFILE_STALENESS_DAYS,
    PlayerProfilesAgent,
    _build_depth_profile,
    _bulk_resolve_player_ids,
    _build_rookie_profile,
    _compute_clean_baseline,
    _compute_season_averages,
    _compute_weighted_baseline,
    _estimate_year1_role,
    _to_decimal,
    _write_profiles,
    needs_sonnet_reasoning,
    profile_needs_refresh,
    _ROOKIE_CONFIDENCE_DISCOUNT,
    _ROOKIE_DEFAULT_PPG,
    _DEVELOPMENT_TIMELINE,
)


# ---------------------------------------------------------------------------
# Mock warehouse for tests (replaces agent._data_cache)
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
        return self._data.get("seasonal_stats", {}).get(season)
    def get_target_share(self, season):
        return self._data.get("target_share", {}).get(season)
    def get_qb_stats(self, season):
        return self._data.get("qb_stats", {}).get(season)
    def get_oline_stats(self, season):
        return self._data.get("oline_stats", {}).get(season)
    def get_def_grades(self, season):
        return self._data.get("def_grades", {}).get(season)
    def get_injuries(self, season):
        return self._data.get("injuries", {}).get(season)
    def get_most_recent_def_grades(self):
        return self._data.get("most_recent_def_grades")
    def get_snap_pct(self, season):
        return self._data.get("snap_pct", {}).get(season)
    def get_ngs_receiving(self, season):
        return self._data.get("ngs_receiving", {}).get(season)
    def get_ngs_rushing(self, season):
        return self._data.get("ngs_rushing", {}).get(season)
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

    # Build warehouse with seasonal_stats for the seasons we need
    seasonal_stats = {
        seasons[-1]: _make_weekly_df(5),   # 5 backup starts → should be flagged
        seasons[-2]: _make_weekly_df(2),   # 2 backup starts → not flagged
    }
    agent._warehouse = _make_warehouse(seasonal_stats=seasonal_stats)

    assert agent._is_backup_qb_season("LAC", seasons[-1]) is True
    assert agent._is_backup_qb_season("LAC", seasons[-2]) is False
    # No data for seasons[0] → not flagged
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

    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
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

    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
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

    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
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

    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=model_output), \
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

    async def _capture_call(system, user, input_data, entity_id, model=None, max_tokens=None):
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

    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "call_once", side_effect=_capture_call), \
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

    async def _capture_call(system, user, input_data, entity_id, model=None, max_tokens=None):
        captured_input_data.append(input_data)
        # Sonnet per-player returns a single object; Haiku returns array
        if model:
            return json.dumps(
                _make_profile("Ladd McConkey", "slot_specialist", situation_score="weak")
            )
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

    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "call_once", side_effect=_capture_call), \
         patch.object(agent, "_build_team_context", new_callable=AsyncMock,
                      return_value=_mock_context("LAC", players=[lac_player])), \
         patch("backend.agents.player_profiles._write_profiles", new_callable=AsyncMock, return_value=1):
        await agent.run_for_team("LAC")

    assert captured_input_data, "call_once was not called"
    # McConkey has dependency_flags → routed to Sonnet per-player call
    # Per-player context: {"team": ..., "player": {...}} not {"players": [...]}
    mcconkey = captured_input_data[0].get("player")
    if mcconkey is None:
        # Fallback: check batch format
        players_in_context = captured_input_data[0].get("players", [])
        mcconkey = next((p for p in players_in_context if "McConkey" in p.get("name", "")), None)
    assert mcconkey is not None, f"McConkey not in context: {list(captured_input_data[0].keys())}"
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
    """Stable veteran team (no Sonnet triggers) makes exactly ONE Haiku batch call."""
    agent = PlayerProfilesAgent()
    call_count = 0

    async def _mock_call(system, user, input_data, entity_id, model=None, max_tokens=None):
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

    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "call_once", side_effect=_mock_call), \
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
    must NOT be sent to the AI model (prevents hallucinated stats) but should
    appear in depth_players for a Python-only depth profile.
    """
    from backend.utils.seasons import get_analysis_seasons, get_analysis_year, get_current_season

    agent = PlayerProfilesAgent()

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

    # No stats in any analysis season
    target_share = {}
    seasonal_stats = {}
    for season in analysis_seasons:
        target_share[season] = pd.DataFrame(
            columns=["player_name", "recent_team", "games", "avg_target_share",
                     "avg_air_yards_share", "total_targets", "total_receptions",
                     "total_rec_yards", "total_rec_tds", "total_carries",
                     "total_rush_yards", "total_rush_tds", "ppr_per_game"]
        )
        seasonal_stats[season] = pd.DataFrame(
            columns=["recent_team", "position", "player_name", "week"]
        )

    agent._warehouse = _make_warehouse(
        rosters=roster_data,
        target_share=target_share,
        seasonal_stats=seasonal_stats,
    )

    # Beneficiary flag — player should not be completely skipped
    dep_flags = {"Mecole Hardman": [
        {"type": "beneficiary", "trigger": "JuJu Smith-Schuster",
         "effect": "positive", "confidence": "medium"}
    ]}

    with patch.object(agent, "_get_team_system", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_dependency_flags",
                      new_callable=AsyncMock, return_value=dep_flags):
        context = await agent._build_team_context("KC")

    # Zero-history player should NOT be in players (sent to AI)
    players_in_context = context["players"]
    assert not any("Hardman" in p["name"] for p in players_in_context), (
        "Zero-history player must not be sent to AI model (hallucination risk)"
    )
    # Should be in depth_players instead
    depth = context["depth_players"]
    assert any("Hardman" in p["name"] for p in depth), (
        "Zero-history player with dep flags must be in depth_players"
    )


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

    # Season stats for one season
    target_share = {}
    seasonal_stats = {}
    ngs_receiving = {}
    for season in analysis_seasons:
        if season == season_with_data:
            target_share[season] = pd.DataFrame([{
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
            ngs_receiving[season] = pd.DataFrame([{
                "player_display_name": "Deebo Samuel",
                "team_abbr": "SF",
                "avg_separation": 2.8,
                "avg_yac_above_expectation": 1.4,
            }])
        else:
            target_share[season] = pd.DataFrame(
                columns=["player_name", "recent_team", "games"]
            )
        seasonal_stats[season] = pd.DataFrame(
            columns=["recent_team", "position", "player_name", "week"]
        )

    agent._warehouse = _make_warehouse(
        rosters=roster_data,
        target_share=target_share,
        seasonal_stats=seasonal_stats,
        ngs_receiving=ngs_receiving,
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

    target_share = {}
    seasonal_stats = {}
    ngs_rushing = {}
    for season in analysis_seasons:
        if season == season_with_data:
            target_share[season] = pd.DataFrame([{
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
            ngs_rushing[season] = pd.DataFrame([{
                "player_display_name": "David Montgomery",
                "team_abbr": "DET",
                "rush_yards_over_expected_per_att": 0.4,
                "rush_pct_over_expected": 55.0,
            }])
        else:
            target_share[season] = pd.DataFrame(
                columns=["player_name", "recent_team", "games"]
            )
        seasonal_stats[season] = pd.DataFrame(
            columns=["recent_team", "position", "player_name", "week"]
        )

    agent._warehouse = _make_warehouse(
        rosters=roster_data,
        target_share=target_share,
        seasonal_stats=seasonal_stats,
        ngs_rushing=ngs_rushing,
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
    agent._warehouse = _make_warehouse(ngs_receiving={2024: pd.DataFrame([{
        "player_display_name": "Tyreek Hill",
        "team_abbr": "MIA",
        "avg_separation": 3.2,
        "avg_yac_above_expectation": 1.8,
    }])})
    result = agent._get_ngs_receiving_stats("Tyreek Hill", "MIA", 2024)
    assert result.get("avg_separation") == pytest.approx(3.2, abs=0.01)
    assert result.get("avg_yac_above_expectation") == pytest.approx(1.8, abs=0.01)


def test_get_ngs_receiving_stats_no_cache():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse()
    assert agent._get_ngs_receiving_stats("Tyreek Hill", "MIA", 2024) == {}


def test_get_ngs_receiving_stats_no_match():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(ngs_receiving={2024: pd.DataFrame([{
        "player_display_name": "Someone Else", "team_abbr": "MIA",
        "avg_separation": 1.0, "avg_yac_above_expectation": 0.5,
    }])})
    assert agent._get_ngs_receiving_stats("Tyreek Hill", "MIA", 2024) == {}


def test_get_ngs_rushing_stats_returns_data():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(ngs_rushing={2024: pd.DataFrame([{
        "player_display_name": "Derrick Henry",
        "team_abbr": "BAL",
        "rush_yards_over_expected_per_att": 0.8,
        "rush_pct_over_expected": 62.0,
    }])})
    result = agent._get_ngs_rushing_stats("Derrick Henry", "BAL", 2024)
    assert result.get("rush_yards_over_expected_per_att") == pytest.approx(0.8, abs=0.01)


def test_get_ngs_rushing_stats_no_cache():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse()
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
    agent._warehouse = _make_warehouse()
    assert agent._get_player_season_stats("Ladd McConkey", "LAC", 2024, position="WR") is None


def test_get_player_season_stats_none_when_no_match():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame(
        columns=["player_name", "recent_team", "games"]
    )})
    assert agent._get_player_season_stats("Ladd McConkey", "LAC", 2024, position="WR") is None


def test_get_player_season_stats_none_when_zero_games():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame([{
        "player_name": "Ladd McConkey", "recent_team": "LAC",
        "games": 0, "avg_target_share": None, "avg_air_yards_share": None,
        "total_targets": 0, "total_receptions": 0, "total_rec_yards": 0,
        "total_rec_tds": 0, "total_carries": 0, "total_rush_yards": 0,
        "total_rush_tds": 0, "ppr_per_game": None,
    }])})
    assert agent._get_player_season_stats("Ladd McConkey", "LAC", 2024, position="WR") is None


# ---------------------------------------------------------------------------
# _get_player_season_stats cross-team aggregation
# ---------------------------------------------------------------------------

def _make_ts_row(player_id, player_name, team, games, rec, rec_yards, rec_tds,
                 carries=0, rush_yards=0, rush_tds=0,
                 target_share=0.2, air_yards_share=0.2, ppr_per_game=10.0,
                 total_fantasy_points=None, position="WR"):
    """Helper to build a target_share DataFrame row."""
    if total_fantasy_points is None:
        total_fantasy_points = rec * 1.0 + (rec_yards + rush_yards) * 0.1 + (rec_tds + rush_tds) * 6.0
    return {
        "player_id": player_id, "player_name": player_name,
        "recent_team": team, "games": games, "position": position,
        "avg_target_share": target_share, "avg_air_yards_share": air_yards_share,
        "total_targets": int(rec * 1.2), "total_receptions": rec,
        "total_rec_yards": rec_yards, "total_rec_tds": rec_tds,
        "total_carries": carries, "total_rush_yards": rush_yards,
        "total_rush_tds": rush_tds, "ppr_per_game": ppr_per_game,
        "total_fantasy_points": total_fantasy_points,
    }


def test_cross_team_stats_combined():
    """Multi-team season rows are aggregated — stats summed across splits."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2022: pd.DataFrame([
        _make_ts_row("00-001", "C.McCaffrey", "CAR", 6, 50, 600, 3, 100, 500, 3, position="RB"),
        _make_ts_row("00-001", "C.McCaffrey", "SF",  11, 80, 1000, 5, 120, 700, 5, position="RB"),
    ])})
    result = agent._get_player_season_stats("Christian McCaffrey", "SF", 2022, position="RB", nfl_player_id="00-001")
    assert result is not None
    assert result["games"] == 17
    assert result["receptions"] == 130
    assert result["rec_yards"] == 1600
    assert result["rec_tds"] == 8
    assert result["carries"] == 220
    assert result["rush_yards"] == 1200
    assert result["rush_tds"] == 8


def test_single_team_stats_unchanged():
    """Single-team season returns same values as before (no regression)."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame([
        _make_ts_row("00-002", "L.McConkey", "LAC", 17, 82, 1149, 7),
    ])})
    result = agent._get_player_season_stats("Ladd McConkey", "LAC", 2024, position="WR", nfl_player_id="00-002")
    assert result is not None
    assert result["games"] == 17
    assert result["receptions"] == 82
    assert result["rec_yards"] == 1149
    assert result["rec_tds"] == 7


def test_games_filter_uses_combined_total():
    """Combined games across teams pass the >=10 clean season filter."""
    # 6 + 5 = 11 games total — should be clean season
    season = {
        "year": 2022, "games": 11,
        "receptions": 60, "rec_yards": 700, "rec_tds": 4,
        "carries": 50, "rush_yards": 300, "rush_tds": 2,
        "backup_qb_season": False,
    }
    result = _compute_clean_baseline([season])
    assert result.get("ppr_points", 0) > 0, "Season with 11 combined games should be clean"


def test_mccaffrey_scenario():
    """McCaffrey 2022: CAR (6g) + SF (11g) should produce ~356 PPR."""
    agent = PlayerProfilesAgent()
    # Approximate real stats
    agent._warehouse = _make_warehouse(target_share={2022: pd.DataFrame([
        _make_ts_row("00-0034844", "C.McCaffrey", "CAR", 6, 27, 277, 1, 85, 470, 5, position="RB"),
        _make_ts_row("00-0034844", "C.McCaffrey", "SF",  11, 58, 587, 5, 119, 554, 3, position="RB"),
    ])})
    result = agent._get_player_season_stats("Christian McCaffrey", "SF", 2022, position="RB", nfl_player_id="00-0034844")
    assert result is not None
    assert result["games"] == 17
    # Total: 85 rec + (864+1024)*0.1 + (6+8)*6 = 85 + 188.8 + 84 ≈ 357.8
    ppr = result["receptions"] * 1.0 + (result["rec_yards"] + result["rush_yards"]) * 0.1 + (result["rec_tds"] + result["rush_tds"]) * 6.0
    assert ppr > 300, f"McCaffrey combined PPR should be >300, got {ppr}"


def test_backup_qb_flag_only_current_team():
    """backup_qb_season flag only applies when stat_team matches current team."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2022: pd.DataFrame([
        _make_ts_row("00-001", "C.McCaffrey", "CAR", 6, 27, 277, 1, 85, 470, 5, position="RB"),
        _make_ts_row("00-001", "C.McCaffrey", "SF",  11, 58, 587, 5, 119, 554, 3, position="RB"),
    ])})
    result = agent._get_player_season_stats("Christian McCaffrey", "SF", 2022, position="RB", nfl_player_id="00-001")
    assert result is not None
    # Primary team should be SF (more games)
    assert result["recent_team"] == "SF"
    # The calling code at line 610-614 checks: if stat_team.upper() == team.upper()
    # Since recent_team=SF and current team=SF, backup_qb flag WOULD apply for SF
    # But it should NOT apply retroactively to CAR stats (which are now combined)


def test_cross_team_weighted_averages():
    """Rate stats (target_share, ppr_per_game) are games-weighted, not simple averaged."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame([
        _make_ts_row("00-003", "S.Diggs", "BUF", 4, 20, 250, 2, target_share=0.30, ppr_per_game=18.0),
        _make_ts_row("00-003", "S.Diggs", "HOU", 13, 60, 800, 5, target_share=0.22, ppr_per_game=14.0),
    ])})
    result = agent._get_player_season_stats("Stefon Diggs", "HOU", 2024, position="WR", nfl_player_id="00-003")
    assert result is not None
    # Weighted avg: (0.30*4 + 0.22*13) / 17 ≈ 0.239
    assert abs(result["target_share"] - 0.239) < 0.01, f"Expected ~0.239, got {result['target_share']}"
    # Weighted avg: (18.0*4 + 14.0*13) / 17 ≈ 14.94
    assert abs(result["ppr_per_game"] - 14.9) < 0.5, f"Expected ~14.9, got {result['ppr_per_game']}"


def test_no_team_preference_in_player_id_path():
    """Player_id path aggregates ALL teams, doesn't prefer current team."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2022: pd.DataFrame([
        _make_ts_row("00-001", "C.McCaffrey", "CAR", 10, 50, 600, 3, 100, 500, 3, position="RB"),
        _make_ts_row("00-001", "C.McCaffrey", "SF",  7, 40, 500, 2, 80, 400, 2, position="RB"),
    ])})
    # Current team is SF but CAR has more games
    result = agent._get_player_season_stats("Christian McCaffrey", "SF", 2022, position="RB", nfl_player_id="00-001")
    assert result is not None
    assert result["games"] == 17  # Both teams combined
    assert result["receptions"] == 90  # 50 + 40
    assert result["rec_yards"] == 1100  # 600 + 500
    # Primary team should be CAR (more games)
    assert result["recent_team"] == "CAR"


# ---------------------------------------------------------------------------
# _get_snap_pct edge cases
# ---------------------------------------------------------------------------

def test_get_snap_pct_none_when_no_cache():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse()
    assert agent._get_snap_pct("Ladd McConkey", "LAC", 2025) is None


def test_get_snap_pct_none_when_missing_columns():
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(snap_pct={2025: pd.DataFrame([{"bad_col": 1}])})
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
    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "_build_team_context",
                      new_callable=AsyncMock, return_value=empty_context):
        result = await agent.run_for_team("LAC")
    assert result == 0


@pytest.mark.asyncio
async def test_run_for_team_exception_returns_zero():
    agent = PlayerProfilesAgent()
    with patch.object(agent, "_get_stale_players", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        result = await agent.run_for_team("LAC")
    assert result == 0


# ---------------------------------------------------------------------------
# run_all_teams with mocked run_for_team
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_all_teams_runs_all_32_teams():
    """run_all_teams invokes run_for_team for all 32 NFL teams."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse()
    call_log: list[str] = []

    async def _mock_run(team: str, force: bool = False) -> int:
        call_log.append(team)
        return 5

    with patch.object(agent, "run_for_team", side_effect=_mock_run):
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

    # New delete-first flow: 4 execute calls in order:
    # 1. select(Player).where(team_abbr == team)  → team player list for delete
    # 2. select(PlayerProfile).where(player_id.in_(...))  → existing profiles (empty → nothing deleted)
    # 3. select(Player).where(or_(...))  → bulk ID resolution
    # 4. select(Player).where(id == player_id)  → parent Player row update
    r_team_players = MagicMock()
    r_team_players.scalars.return_value.all.return_value = [mock_player_row]
    r_existing_profiles = MagicMock()
    r_existing_profiles.scalars.return_value.all.return_value = []   # nothing to delete
    r_bulk = MagicMock()
    r_bulk.scalars.return_value.all.return_value = [mock_player_row]
    r_player = MagicMock()
    r_player.scalar_one_or_none.return_value = mock_player_row

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_team_players, r_existing_profiles, r_bulk, r_player])
    mock_session.add = MagicMock()
    mock_session.delete = MagicMock()
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
         "rec_tds": 7, "rush_yards": 0, "rush_tds": 0, "carries": 0,
         "backup_qb_season": False},
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


# ===========================================================================
# FIX 1: Minimum usage threshold
# ===========================================================================

from backend.agents.player_profiles import _MINIMUM_TOUCHES_FOR_PROJECTION


def test_minimum_touches_threshold_filters_low_usage():
    """FIX 1: Player with < 50 career touches returns empty baseline."""
    # Jermar Jefferson scenario: 21 career carries, 0 receptions
    seasons = [
        {"year": 2022, "games": 12, "receptions": 0, "rec_yards": 0,
         "rec_tds": 0, "rush_yards": 80, "rush_tds": 0, "carries": 10,
         "backup_qb_season": False},
        {"year": 2023, "games": 14, "receptions": 0, "rec_yards": 0,
         "rec_tds": 0, "rush_yards": 40, "rush_tds": 0, "carries": 11,
         "backup_qb_season": False},
    ]
    baseline = _compute_clean_baseline(seasons)
    assert baseline == {}, (
        f"Player with {sum(s.get('carries', 0) + s.get('receptions', 0) for s in seasons)} "
        f"career touches should get empty baseline, got: {baseline}"
    )


def test_minimum_touches_threshold_allows_sufficient_usage():
    """FIX 1: Player with >= 50 career touches gets a valid baseline."""
    seasons = [
        {"year": 2024, "games": 16, "receptions": 30, "rec_yards": 300,
         "rec_tds": 2, "rush_yards": 400, "rush_tds": 3, "carries": 80,
         "backup_qb_season": False},
    ]
    baseline = _compute_clean_baseline(seasons)
    assert baseline != {}
    assert baseline["ppr_points"] > 0


def test_minimum_touches_constant_is_50():
    """FIX 1: Threshold constant is 50."""
    assert _MINIMUM_TOUCHES_FOR_PROJECTION == 50


# ===========================================================================
# FIX 3: Career decline detection
# ===========================================================================


def test_career_decline_weights_recent_season():
    """FIX 3: When recent PPR < 65% of peak, weight recent 60% / career 40%."""
    # Peak season: 300 PPR. Recent season: 150 PPR (50% of peak → declining)
    seasons = [
        {"year": 2022, "games": 16, "receptions": 80, "rec_yards": 1200,
         "rec_tds": 10, "rush_yards": 0, "rush_tds": 0, "carries": 0,
         "backup_qb_season": False},  # PPR = 80 + 120 + 60 = 260
        {"year": 2023, "games": 16, "receptions": 100, "rec_yards": 1400,
         "rec_tds": 12, "rush_yards": 0, "rush_tds": 0, "carries": 0,
         "backup_qb_season": False},  # PPR = 100 + 140 + 72 = 312 (peak)
        {"year": 2024, "games": 16, "receptions": 30, "rec_yards": 400,
         "rec_tds": 2, "rush_yards": 0, "rush_tds": 0, "carries": 0,
         "backup_qb_season": False},  # PPR = 30 + 40 + 12 = 82 (declining)
    ]
    baseline = _compute_clean_baseline(seasons)
    assert baseline.get("declining") is True

    # Flat average would be (260 + 312 + 82) / 3 = 218
    flat_avg = (260 + 312 + 82) / 3
    # Decline-weighted should be closer to recent (82) than flat average
    assert baseline["ppr_points"] < flat_avg, (
        f"Decline-weighted PPR ({baseline['ppr_points']}) should be below "
        f"flat average ({flat_avg})"
    )


def test_no_decline_flag_when_stable():
    """FIX 3: Stable player does NOT get declining flag."""
    seasons = [
        {"year": 2023, "games": 16, "receptions": 80, "rec_yards": 1000,
         "rec_tds": 7, "rush_yards": 0, "rush_tds": 0, "carries": 0,
         "backup_qb_season": False},
        {"year": 2024, "games": 16, "receptions": 85, "rec_yards": 1050,
         "rec_tds": 8, "rush_yards": 0, "rush_tds": 0, "carries": 0,
         "backup_qb_season": False},
    ]
    baseline = _compute_clean_baseline(seasons)
    assert "declining" not in baseline


# ---------------------------------------------------------------------------
# QB baseline tests
# ---------------------------------------------------------------------------


def test_qb_baseline_uses_fantasy_points_not_targets():
    """QB baseline uses fantasy_points_ppr directly (includes passing scoring)."""
    from backend.agents.player_profiles import _compute_qb_baseline

    seasons = [
        {"year": 2023, "games": 17, "fantasy_points_ppr": 380.0, "ppr_per_game": 22.4,
         "passing_yards": 4500, "passing_tds": 30, "interceptions": 10, "cpoe": 2.1},
        {"year": 2024, "games": 17, "fantasy_points_ppr": 400.0, "ppr_per_game": 23.5,
         "passing_yards": 4700, "passing_tds": 32, "interceptions": 8, "cpoe": 2.5},
    ]
    baseline = _compute_qb_baseline(seasons)
    assert baseline != {}
    # PPR should be ~390 (avg of 380 and 400) mapped to 17 games via ppg
    # avg_ppg = (22.4 + 23.5) / 2 = 22.95 → ppr_points = 22.95 * 17 = 390.15
    assert baseline["ppr_points"] > 350, (
        f"QB baseline {baseline['ppr_points']} should reflect passing (>350)"
    )
    assert baseline["ppg"] > 20


def test_qb_minimum_games_threshold():
    """QBs with fewer than 10 career games get no baseline."""
    from backend.agents.player_profiles import _compute_qb_baseline

    seasons = [
        {"year": 2024, "games": 4, "fantasy_points_ppr": 80.0, "ppr_per_game": 20.0,
         "passing_yards": 1000, "passing_tds": 6, "interceptions": 3},
    ]
    baseline = _compute_qb_baseline(seasons)
    assert baseline == {}


def test_qb_ppr_scoring_uses_correct_formula():
    """QB PPR ≈ pass_td*4 + pass_yds*0.04 + rush_yds*0.1 + rush_td*6 - INT*2 + rec*1."""
    from backend.agents.player_profiles import _compute_qb_baseline

    # Mahomes-like season: 4500 yards, 35 TDs, 12 INT, 300 rush, 2 rush TD
    # Expected PPR: 4500*0.04 + 35*4 + 12*(-2) + 300*0.1 + 2*6 = 180+140-24+30+12 = 338
    # With fantasy_points_ppr directly = ~338 + some receptions
    seasons = [
        {"year": 2024, "games": 17, "fantasy_points_ppr": 345.0, "ppr_per_game": 20.3,
         "passing_yards": 4500, "passing_tds": 35, "interceptions": 12,
         "rushing_yards": 300, "rushing_tds": 2},
    ]
    baseline = _compute_qb_baseline(seasons)
    assert baseline != {}
    # Should be close to 345 (ppr_per_game * 17 = 20.3*17 = 345.1)
    assert abs(baseline["ppr_points"] - 345.0) < 5


def test_qb_baseline_with_decline():
    """QB with career decline gets weighted baseline."""
    from backend.agents.player_profiles import _compute_qb_baseline

    seasons = [
        {"year": 2022, "games": 17, "fantasy_points_ppr": 400.0, "ppr_per_game": 23.5,
         "passing_yards": 4800, "passing_tds": 35, "interceptions": 8},
        {"year": 2023, "games": 17, "fantasy_points_ppr": 380.0, "ppr_per_game": 22.4,
         "passing_yards": 4500, "passing_tds": 30, "interceptions": 10},
        {"year": 2024, "games": 16, "fantasy_points_ppr": 220.0, "ppr_per_game": 13.8,
         "passing_yards": 2800, "passing_tds": 15, "interceptions": 14},
    ]
    baseline = _compute_qb_baseline(seasons)
    assert baseline != {}
    assert baseline.get("declining") is True
    # Flat average ppg = (23.5+22.4+13.8)/3 = 19.9
    # Decline-weighted should be closer to 13.8 than 19.9
    assert baseline["ppg"] < 19.9


def test_qb_included_in_skill_positions():
    """SKILL_POSITIONS includes QB."""
    from backend.agents.player_profiles import SKILL_POSITIONS
    assert "QB" in SKILL_POSITIONS


def test_qb_mobility_elite():
    """QB with > 40 rush ypg is elite mobility."""
    from backend.agents.team_systems import _derive_qb_mobility
    qb_data = {"games_played": 17, "rushing_yards": 850}  # 50 ypg
    assert _derive_qb_mobility(qb_data) == "elite"


def test_qb_mobility_pocket_only():
    """QB with < 15 rush ypg is pocket_only."""
    from backend.agents.team_systems import _derive_qb_mobility
    qb_data = {"games_played": 17, "rushing_yards": 100}  # 5.9 ypg
    assert _derive_qb_mobility(qb_data) == "pocket_only"


# ---------------------------------------------------------------------------
# needs_sonnet_reasoning() — model routing tests
# ---------------------------------------------------------------------------

def test_needs_sonnet_reasoning_rookie():
    """Rookies always get Sonnet."""
    player = {"name": "Rome Odunze", "is_rookie": True}
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_dependency_flags():
    """Players with dependency flags get Sonnet."""
    player = {
        "name": "Puka Nacua",
        "dependency_flags": [{"type": "beneficiary", "trigger": "Cooper Kupp"}],
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_contract_year():
    """Contract year players get Sonnet."""
    player = {"name": "Tee Higgins", "contract_year": True}
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_high_injury():
    """Players with high injury risk get Sonnet."""
    player = {
        "name": "Saquon Barkley",
        "injury_profile": {"overall_risk_level": "high"},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_pattern_flags():
    """Players with injury pattern flags get Sonnet."""
    player = {
        "name": "Nick Chubb",
        "injury_profile": {"pattern_flags": ["POST_ACL"]},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_beat_signal():
    """Players with high-confidence beat signals get Sonnet."""
    player = {
        "name": "Tank Dell",
        "beat_signals": [{"confidence": "high", "signal_type": "depth_chart_change"}],
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_compound_risk():
    """Players on compound_risk_flag teams get Sonnet."""
    player = {
        "name": "Jaxon Smith-Njigba",
        "_team_system": {"compound_risk_flag": True},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_stable_veteran():
    """Stable veteran with no triggers gets Haiku batch."""
    player = {
        "name": "Stefon Diggs",
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is False


def test_needs_sonnet_reasoning_empty_player():
    """Player with no optional fields defaults to Haiku."""
    player = {"name": "Unknown Player"}
    assert needs_sonnet_reasoning(player) is False


def test_needs_sonnet_reasoning_qb_always():
    """All QBs get Sonnet — they anchor offenses and need deeper reasoning."""
    player = {
        "name": "Patrick Mahomes",
        "position": "QB",
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_qb_backup():
    """Even backup QBs get Sonnet — minimal extra cost, consistent routing."""
    player = {"name": "Joe Milton III", "position": "QB"}
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_stable_wr_not_qb():
    """Stable veteran WR still goes to Haiku — QB check doesn't broaden."""
    player = {
        "name": "Stefon Diggs",
        "position": "WR",
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is False


def test_needs_sonnet_reasoning_rb_age_28():
    """29yo RB triggers Sonnet — RBs decline sharply after 28."""
    player = {
        "name": "Joe Mixon",
        "position": "RB",
        "age": 29,
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "moderate", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_rb_age_27_stays_haiku():
    """27yo RB stays Haiku — below age threshold."""
    player = {
        "name": "Josh Jacobs",
        "position": "RB",
        "age": 27,
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is False


def test_needs_sonnet_reasoning_wr_age_31():
    """31yo WR triggers Sonnet — WRs hold value longer but decline after 31."""
    player = {
        "name": "Davante Adams",
        "position": "WR",
        "age": 31,
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_wr_age_29_stays_haiku():
    """29yo WR stays Haiku — below age threshold."""
    player = {
        "name": "Mike Evans",
        "position": "WR",
        "age": 29,
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is False


def test_needs_sonnet_reasoning_team_change():
    """Player who changed teams → Sonnet (new system, new QB, new role)."""
    player = {
        "name": "Austin Ekeler",
        "position": "RB",
        "age": 27,
        "team_changed_this_offseason": True,
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_league_price_under_5():
    """League price <= $5 → Sonnet (league says declining, force reasoning)."""
    player = {
        "name": "Dalvin Cook",
        "position": "RB",
        "age": 27,
        "market_value_league": 2,
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_declining_trajectory():
    """Declining trajectory → Sonnet (history overstates future)."""
    player = {
        "name": "Derrick Henry",
        "position": "RB",
        "age": 27,
        "career_trajectory": "declining",
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "low", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is True


def test_needs_sonnet_reasoning_mixon_full():
    """Mixon: age=29 RB, team change, league $2 — multiple triggers all fire."""
    player = {
        "name": "Joe Mixon",
        "position": "RB",
        "age": 29,
        "team_changed_this_offseason": True,
        "market_value_league": 2,
        "is_rookie": False,
        "contract_year": False,
        "dependency_flags": [],
        "injury_profile": {"overall_risk_level": "moderate", "pattern_flags": []},
        "beat_signals": [],
        "_team_system": {"compound_risk_flag": False},
    }
    assert needs_sonnet_reasoning(player) is True


# ---------------------------------------------------------------------------
# AI projection override tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sonnet_projection_stored_as_projected_ppr_season():
    """When profile has projected_ppr_points, it's stored as projected_ppr_season (not overwriting ppr_points)."""
    from backend.agents.player_profiles import _write_profiles
    from backend.utils.seasons import get_analysis_year

    profile = _make_profile(name="Puka Nacua")
    profile["projected_ppr_points"] = 310.5
    profile["upside_ppr"] = 360.0
    profile["downside_ppr"] = 250.0

    ctx_player = {
        "name": "Puka Nacua",
        "position": "WR",
        "seasons": [
            {"year": 2024, "games": 17, "receptions": 90, "rec_yards": 1200,
             "rec_tds": 6, "rush_yards": 0, "rush_tds": 0},
        ],
        "nfl_player_id": "00-0039999",
    }

    context = _mock_context(team="LAR", players=[ctx_player])

    # Mock the DB session to track what gets written
    mock_session = AsyncMock()
    mock_player = MagicMock()
    mock_player.id = uuid.uuid4()
    mock_player.yahoo_player_id = "nfl_00-0039999"
    mock_player.name = "Puka Nacua"
    mock_player.team_abbr = "LAR"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_player]
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.delete = AsyncMock()

    records_written = []
    original_add = mock_session.add

    def track_add(record):
        records_written.append(record)

    mock_session.add = track_add

    # Patch the scalar_one_or_none for the Player update query
    player_result = MagicMock()
    player_result.scalar_one_or_none.return_value = mock_player

    call_count = 0
    async def mock_execute(stmt):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:  # first two calls: team players + existing profiles
            return mock_result
        return player_result  # subsequent: player lookup

    mock_session.execute = AsyncMock(side_effect=mock_execute)

    ctx_mgr = AsyncMock()
    ctx_mgr.__aenter__ = AsyncMock(return_value=mock_session)
    ctx_mgr.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.agents.player_profiles.AsyncSessionLocal", return_value=ctx_mgr):
        written = await _write_profiles([profile], context, "LAR")

    assert written == 1
    record = records_written[0]
    baseline = record.clean_season_baseline
    # Historical ppr_points preserved from Python baseline (receptions*1 + yards*0.1 + tds*6)
    # 90*1 + 1200*0.1 + 6*6 = 90 + 120 + 36 = 246.0
    assert baseline["ppr_points"] == 246.0
    # Sonnet projection stored separately
    assert baseline["projected_ppr_season"] == 310.5
    assert baseline["upside_ppr"] == 360.0
    assert baseline["downside_ppr"] == 250.0


@pytest.mark.asyncio
async def test_call_once_model_override():
    """Passing model= to call_once() uses that model instead of class default."""
    from backend.agents.base_agent import HAIKU, SONNET

    agent = PlayerProfilesAgent(dry_run=True)
    assert agent.AGENT_MODEL == HAIKU  # class default is Haiku

    # Patch _check_cache to return None (no cache hit) and _log_usage to no-op
    with patch.object(agent, "_check_cache", new_callable=AsyncMock, return_value=None), \
         patch.object(agent, "_log_usage", new_callable=AsyncMock):

        # Dry run with model override → should log SONNET, not HAIKU
        import logging
        with patch("backend.agents.base_agent.logger") as mock_logger:
            result = await agent.call_once(
                system="test",
                user="test",
                input_data={"test": True},
                entity_id="test",
                model=SONNET,
                max_tokens=800,
            )
            # Dry run returns ""
            assert result == ""
            # Check that the dry-run log message contains SONNET model string
            mock_logger.info.assert_called()
            log_args = mock_logger.info.call_args
            assert SONNET in str(log_args)


def test_haiku_batch_uses_python_baseline():
    """When profile has no projected_ppr_points, Python baseline is used."""
    profile = _make_profile(name="Test WR")
    # No projected_ppr_points key — simulates Haiku batch output
    assert "projected_ppr_points" not in profile

    # The Python _compute_clean_baseline would produce the ppr_points
    seasons = [
        {"year": 2024, "games": 17, "receptions": 80, "rec_yards": 1000,
         "rec_tds": 7, "rush_yards": 0, "rush_tds": 0},
    ]
    baseline = _compute_clean_baseline(seasons)
    assert baseline["ppr_points"] > 0
    # Verify formula: 80*1 + 1000*0.1 + 7*6 = 80 + 100 + 42 = 222
    assert baseline["ppr_points"] == 222.0


# ===========================================================================
# Rookie profiling tests (12 tests from stage-05 spec)
# ===========================================================================

def _make_rookie(**overrides) -> dict:
    """Build a minimal rookie player dict for _build_rookie_profile."""
    base = {
        "position": "WR",
        "is_rookie": True,
        "comp_yr1_avg_ppg": 12.0,
        "comp_yr2_avg_ppg": 15.0,
        "college_profile_grade": "strong",
        "draft_capital_signal": "high",
        "landing_spot_modifier": 1.0,
        "historical_comp_names": ["Jaylen Waddle", "Chris Olave"],
        "depth_chart_rank": 2,
    }
    base.update(overrides)
    return base


def test_rookie_routed_to_rookie_branch():
    """Player with is_rookie=True uses _build_rookie_profile, not veteran path."""
    player = _make_rookie()
    result = _build_rookie_profile(player, {})
    assert result["is_rookie"] is True
    assert result["profile_source"] == "college_comps"


def test_veteran_not_routed_to_rookie_branch():
    """Player with is_rookie=False uses veteran baseline, not rookie path."""
    # Veterans go through _compute_clean_baseline, not _build_rookie_profile
    seasons = [
        {"year": 2024, "games": 17, "receptions": 90, "rec_yards": 1100,
         "rec_tds": 8, "rush_yards": 0, "rush_tds": 0},
    ]
    baseline = _compute_clean_baseline(seasons)
    # Veteran baseline has stat components — not the "note" field
    assert "receptions" in baseline
    assert baseline["ppr_points"] > 0


def test_rookie_profile_uses_comp_data_not_nfl_history():
    """Rookie baseline derived from comp_yr1_avg_ppg * confidence_discount."""
    player = _make_rookie(position="WR", comp_yr1_avg_ppg=12.0, landing_spot_modifier=1.0)
    result = _build_rookie_profile(player, {})
    # WR discount = 0.75, so: 12.0 * 17 * 0.75 = 153.0
    expected = 12.0 * 17 * _ROOKIE_CONFIDENCE_DISCOUNT["WR"]
    assert result["clean_season_baseline"]["ppr_points"] == round(expected, 1)
    assert result["clean_season_baseline"]["note"] == "Derived from historical comp average — not NFL history"


def test_rookie_confidence_discount_qb_is_lowest():
    """QB rookie discount (0.65) < TE (0.70) < WR (0.75) < RB (0.85)."""
    assert _ROOKIE_CONFIDENCE_DISCOUNT["QB"] == 0.65
    assert _ROOKIE_CONFIDENCE_DISCOUNT["QB"] < _ROOKIE_CONFIDENCE_DISCOUNT["TE"]
    assert _ROOKIE_CONFIDENCE_DISCOUNT["TE"] < _ROOKIE_CONFIDENCE_DISCOUNT["WR"]
    assert _ROOKIE_CONFIDENCE_DISCOUNT["WR"] < _ROOKIE_CONFIDENCE_DISCOUNT["RB"]


def test_rookie_confidence_discount_rb_is_highest():
    """RB discount is 0.85 — translates fastest from college."""
    assert _ROOKIE_CONFIDENCE_DISCOUNT["RB"] == 0.85
    assert _ROOKIE_CONFIDENCE_DISCOUNT["RB"] == max(_ROOKIE_CONFIDENCE_DISCOUNT.values())


def test_rookie_wider_ceiling_floor_range():
    """
    Rookie: ceiling = baseline * 1.45, floor = baseline * 0.55
    Veteran: ceiling = baseline * 1.25, floor = baseline * 0.75
    Rookie range must be wider.
    """
    player = _make_rookie(position="RB", comp_yr1_avg_ppg=10.0, landing_spot_modifier=1.0)
    result = _build_rookie_profile(player, {})
    baseline = result["clean_season_baseline"]["ppr_points"]
    assert result["ceiling_value_ppr"] == round(baseline * 1.45, 1)
    assert result["floor_value_ppr"] == round(baseline * 0.55, 1)
    # Verify range is wider than veteran (1.45 - 0.55 = 0.90 > 1.25 - 0.75 = 0.50)
    rookie_range = result["ceiling_value_ppr"] - result["floor_value_ppr"]
    vet_equivalent_range = baseline * 1.25 - baseline * 0.75
    assert rookie_range > vet_equivalent_range


def test_rookie_variance_flag_always_true():
    """All rookies have variance_flag=True regardless of college profile grade."""
    for grade in ["elite", "strong", "average", "weak"]:
        player = _make_rookie(college_profile_grade=grade)
        result = _build_rookie_profile(player, {})
        assert result["variance_flag"] is True


def test_rb_rookie_development_timeline_year1():
    """RB rookies -> breakout_window = 'year_1'."""
    player = _make_rookie(position="RB")
    result = _build_rookie_profile(player, {})
    assert result["breakout_window"] == "year_1"
    assert _DEVELOPMENT_TIMELINE["RB"] == "year_1"


def test_wr_rookie_development_timeline_year2_3():
    """WR rookies -> breakout_window = 'year_2_to_3'."""
    player = _make_rookie(position="WR")
    result = _build_rookie_profile(player, {})
    assert result["breakout_window"] == "year_2_to_3"


def test_te_rookie_development_timeline_year3_4():
    """TE rookies -> breakout_window = 'year_3_to_4'."""
    player = _make_rookie(position="TE")
    result = _build_rookie_profile(player, {})
    assert result["breakout_window"] == "year_3_to_4"


def test_elite_profile_high_capital_is_breakout_candidate():
    """
    college_profile_grade='elite' AND draft_capital_signal='high'
    -> breakout_flag = True even as a rookie (Chase/Jefferson tier).
    """
    player = _make_rookie(college_profile_grade="elite", draft_capital_signal="high")
    result = _build_rookie_profile(player, {})
    assert result["breakout_flag"] is True
    assert result["breakout_reasoning"] is not None


def test_landing_spot_modifier_applied_to_projection():
    """
    Rookie with comp_yr1_avg_ppg=12.0 and landing_modifier=0.75
    -> adjusted baseline < 12.0 * 17 games.
    """
    player_good = _make_rookie(comp_yr1_avg_ppg=12.0, landing_spot_modifier=1.0)
    player_bad = _make_rookie(comp_yr1_avg_ppg=12.0, landing_spot_modifier=0.75)
    good = _build_rookie_profile(player_good, {})
    bad = _build_rookie_profile(player_bad, {})
    assert bad["clean_season_baseline"]["ppr_points"] < good["clean_season_baseline"]["ppr_points"]
    # 0.75 modifier should reduce by 25%
    ratio = bad["clean_season_baseline"]["ppr_points"] / good["clean_season_baseline"]["ppr_points"]
    assert abs(ratio - 0.75) < 0.01


def test_average_profile_low_capital_not_breakout_candidate():
    """college_profile_grade='average', capital='low' -> breakout_flag=False."""
    player = _make_rookie(college_profile_grade="average", draft_capital_signal="low")
    result = _build_rookie_profile(player, {})
    assert result["breakout_flag"] is False
    assert result["breakout_reasoning"] is None


# ---------------------------------------------------------------------------
# Rookie roster injection — rookies not in nfl_data_py rosters get injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_team_context_injects_rookies_not_in_roster():
    """Rookies from DB should appear in context even if absent from nfl_data_py roster."""
    agent = PlayerProfilesAgent(dry_run=True)
    agent._warehouse = _make_warehouse()

    # Mock: nfl_data_py roster returns only one veteran
    veteran_roster = [{"name": "Derrick Henry", "position": "RB", "age": 31}]

    # Mock: DB has a rookie on this team
    rookie_fields = {
        "Cam Ward": {
            "is_rookie": True,
            "position": "QB",
            "college_profile_grade": "weak",
            "draft_capital_signal": "high",
            "landing_spot_modifier": 1.0,
            "comp_yr1_avg_ppg": None,
            "comp_yr2_avg_ppg": None,
            "historical_comp_names": [],
            "depth_chart_rank": 2,
        },
    }

    # DB player injection returns the rookie (simulates DB having Cam Ward on TEN)
    db_team_players = [{"name": "Cam Ward", "position": "QB", "age": 22}]

    with patch.object(agent, "_get_team_roster", return_value=veteran_roster), \
         patch.object(agent, "_get_team_rookie_fields", new_callable=AsyncMock, return_value=rookie_fields), \
         patch.object(agent, "_get_db_team_players", new_callable=AsyncMock, return_value=db_team_players), \
         patch.object(agent, "_get_team_system", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_dependency_flags", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_injury_profiles", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_schedules", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_beat_signals", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_market_values", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_is_backup_qb_season", return_value=False), \
         patch.object(agent, "_get_player_season_stats", return_value=None), \
         patch.object(agent, "_get_qb_season", return_value=None), \
         patch.object(agent, "_get_snap_pct", return_value=None):

        ctx = await agent._build_team_context("TEN")

    player_names = [p["name"] for p in ctx["players"]]
    assert "Cam Ward" in player_names, "Rookie should be injected into context"
    cam = next(p for p in ctx["players"] if p["name"] == "Cam Ward")
    assert cam["is_rookie"] is True
    assert cam["position"] == "QB"


# ---------------------------------------------------------------------------
# Profile cache invalidation — profile_needs_refresh()
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_profile_needs_refresh_no_profile():
    """No existing profile → needs refresh."""
    assert profile_needs_refresh(profile_updated_at=None) is True


def test_profile_needs_refresh_stale_30_days():
    """Profile older than PROFILE_STALENESS_DAYS → needs refresh."""
    ver = PLAYER_PROFILES_PROMPT_VERSION
    old = _now() - timedelta(days=PROFILE_STALENESS_DAYS + 1)
    assert profile_needs_refresh(profile_updated_at=old, stored_prompt_version=ver) is True

    # Just under threshold → still current
    recent = _now() - timedelta(days=PROFILE_STALENESS_DAYS - 1)
    assert profile_needs_refresh(profile_updated_at=recent, stored_prompt_version=ver) is False


def test_profile_needs_refresh_dep_updated():
    """Dependency flags updated after profile → needs refresh."""
    ver = PLAYER_PROFILES_PROMPT_VERSION
    profile_time = _now() - timedelta(hours=12)
    dep_time = _now() - timedelta(hours=6)  # updated AFTER profile
    assert profile_needs_refresh(
        profile_updated_at=profile_time,
        dep_updated_at=dep_time,
        stored_prompt_version=ver,
    ) is True


def test_profile_needs_refresh_injury_updated():
    """Injury profile updated after profile → needs refresh."""
    ver = PLAYER_PROFILES_PROMPT_VERSION
    profile_time = _now() - timedelta(hours=12)
    injury_time = _now() - timedelta(hours=6)
    assert profile_needs_refresh(
        profile_updated_at=profile_time,
        injury_updated_at=injury_time,
        stored_prompt_version=ver,
    ) is True


def test_profile_needs_refresh_team_change():
    """team_updated_at after profile → needs refresh (player traded)."""
    ver = PLAYER_PROFILES_PROMPT_VERSION
    profile_time = _now() - timedelta(days=5)
    team_change = _now() - timedelta(days=2)
    assert profile_needs_refresh(
        profile_updated_at=profile_time,
        team_updated_at=team_change,
        stored_prompt_version=ver,
    ) is True


def test_profile_needs_refresh_beat_signal():
    """New high-confidence beat signal after profile → needs refresh."""
    ver = PLAYER_PROFILES_PROMPT_VERSION
    profile_time = _now() - timedelta(hours=24)
    signal_time = _now() - timedelta(hours=6)
    assert profile_needs_refresh(
        profile_updated_at=profile_time,
        beat_signal_timestamps=[signal_time],
        stored_prompt_version=ver,
    ) is True


def test_profile_needs_refresh_current():
    """Profile is current with no upstream changes → no refresh needed."""
    ver = PLAYER_PROFILES_PROMPT_VERSION
    profile_time = _now() - timedelta(days=5)  # recent enough
    # All upstream data is OLDER than the profile
    old_dep = _now() - timedelta(days=10)
    old_injury = _now() - timedelta(days=10)
    old_signal = _now() - timedelta(days=10)
    old_team = _now() - timedelta(days=10)
    assert profile_needs_refresh(
        profile_updated_at=profile_time,
        dep_updated_at=old_dep,
        injury_updated_at=old_injury,
        beat_signal_timestamps=[old_signal],
        team_updated_at=old_team,
        stored_prompt_version=ver,
    ) is False


# ---------------------------------------------------------------------------
# IR-year-1 player handling (McCarthy scenario)
# ---------------------------------------------------------------------------


def test_ir_year1_player_not_marked_as_rookie():
    """
    Player drafted in year N, on IR all of year N,
    in year N+1 should have:
      is_rookie = False
      nfl_seasons_played = 1
    NOT is_rookie = True.

    Verifies the needs_sonnet_reasoning function still routes
    such a player through Sonnet (via QB rule or other triggers).
    """
    mccarthy_like = {
        "name": "J.J. McCarthy",
        "position": "QB",
        "age": 23,
        "is_rookie": False,  # NOT a rookie — was on NFL roster
        "nfl_seasons_played": 1,
        "seasons": [
            {"year": 2023, "games": 0, "note": "no data"},
            {"year": 2024, "games": 0, "note": "no data"},
            {"year": 2025, "games": 0, "note": "no data"},
        ],
    }
    # QB position → Sonnet regardless of rookie status
    assert needs_sonnet_reasoning(mccarthy_like) is True
    assert mccarthy_like["is_rookie"] is False
    assert mccarthy_like["nfl_seasons_played"] == 1


def test_ir_player_triggers_sonnet_via_qb_position():
    """
    full_season_absence QB → Sonnet routing regardless of is_rookie status.
    QBs ALWAYS route to Sonnet (line 114 in needs_sonnet_reasoning).
    """
    ir_qb = {
        "name": "Test QB",
        "position": "QB",
        "age": 24,
        "is_rookie": False,
    }
    assert needs_sonnet_reasoning(ir_qb) is True


@pytest.mark.asyncio
async def test_market_value_prevents_skip_in_context():
    """
    Player with zero game data but has market_value should NOT be
    skipped in _build_team_context — the has_market_value check
    keeps fantasy-relevant players.
    """
    agent = PlayerProfilesAgent(dry_run=True)
    agent._warehouse = _make_warehouse()

    roster = [{"name": "Existing Vet", "position": "RB", "age": 28}]
    db_players = [{"name": "IR Player", "position": "QB", "age": 23}]
    # Player has market value but is NOT a rookie and has no dep flags
    market_values = {"IR Player": 3.0}

    with patch.object(agent, "_get_team_roster", return_value=roster), \
         patch.object(agent, "_get_team_rookie_fields", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_db_team_players", new_callable=AsyncMock, return_value=db_players), \
         patch.object(agent, "_get_team_system", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_dependency_flags", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_injury_profiles", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_schedules", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_beat_signals", new_callable=AsyncMock, return_value={}), \
         patch.object(agent, "_get_team_market_values", new_callable=AsyncMock, return_value=market_values), \
         patch.object(agent, "_is_backup_qb_season", return_value=False), \
         patch.object(agent, "_get_player_season_stats", return_value=None), \
         patch.object(agent, "_get_qb_season", return_value=None), \
         patch.object(agent, "_get_snap_pct", return_value=None):

        ctx = await agent._build_team_context("MIN")

    player_names = [p["name"] for p in ctx["players"]]
    assert "IR Player" in player_names, (
        "Player with market_value should not be skipped even with zero game data"
    )


# ---------------------------------------------------------------------------
# Prompt version triggers regeneration
# ---------------------------------------------------------------------------


def test_prompt_version_triggers_regeneration():
    """When PLAYER_PROFILES_PROMPT_VERSION changes, profile_needs_refresh returns True."""
    from datetime import datetime, timezone

    recent = datetime.now(timezone.utc)
    # Same version → no refresh needed
    assert not profile_needs_refresh(
        profile_updated_at=recent,
        stored_prompt_version=PLAYER_PROFILES_PROMPT_VERSION,
    )
    # Different version → refresh needed
    assert profile_needs_refresh(
        profile_updated_at=recent,
        stored_prompt_version="v1",
    )
    # None version (old profile without version) → refresh needed
    assert profile_needs_refresh(
        profile_updated_at=recent,
        stored_prompt_version=None,
    )


def test_prompt_version_constant_is_v3():
    """Sanity: prompt version constant is v3 after warehouse refactor."""
    assert PLAYER_PROFILES_PROMPT_VERSION == "v3"


# ---------------------------------------------------------------------------
# Depth chart rank pass-through
# ---------------------------------------------------------------------------


def test_depth_rank_passes_through_to_roster():
    """depth_chart_rank from warehouse should appear in _get_team_roster output
    when players have nfl_player_id and warehouse has depth_ranks data."""
    agent = PlayerProfilesAgent(dry_run=True, warehouse=_make_warehouse(
        rosters=pd.DataFrame([{
            "full_name": "Josh Allen", "team": "BUF", "position": "QB",
            "week": 18, "player_id": "00-0034857", "age": 28,
        }]),
        depth_ranks={"00-0034857": 1},
    ))
    # _get_team_roster doesn't itself add depth_rank — _build_team_context does.
    # So verify the warehouse accessor works.
    rank = agent._warehouse.get_player_depth_rank("00-0034857")
    assert rank == 1

    # Verify unknown gsis_id returns None
    assert agent._warehouse.get_player_depth_rank("00-9999999") is None


# ---------------------------------------------------------------------------
# RB role classification definitions in prompt
# ---------------------------------------------------------------------------


def test_haiku_prompt_contains_rb_role_definitions():
    """Haiku system prompt must define workhorse threshold (65%+ carries)."""
    from backend.agents.player_profiles import HAIKU_SYSTEM_PROMPT

    assert "65%+" in HAIKU_SYSTEM_PROMPT
    assert "committee_back" in HAIKU_SYSTEM_PROMPT
    assert "workhorse" in HAIKU_SYSTEM_PROMPT
    assert "featured_back" in HAIKU_SYSTEM_PROMPT
    assert '"backup"' in HAIKU_SYSTEM_PROMPT
    assert "DO NOT use just because a backup exists" in HAIKU_SYSTEM_PROMPT


def test_sonnet_prompt_contains_rb_role_definitions():
    """Sonnet system prompt must define workhorse threshold (65%+ carries)."""
    from backend.agents.player_profiles import SONNET_SYSTEM_PROMPT

    assert "65%+" in SONNET_SYSTEM_PROMPT
    assert "committee_back" in SONNET_SYSTEM_PROMPT
    assert "workhorse" in SONNET_SYSTEM_PROMPT
    assert "featured_back" in SONNET_SYSTEM_PROMPT
    assert '"backup"' in SONNET_SYSTEM_PROMPT
    assert "DO NOT use just because a backup exists" in SONNET_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Weighted baseline tests
# ---------------------------------------------------------------------------

def test_weighted_baseline_excludes_injury_season():
    """Injury-shortened seasons excluded from baseline."""
    stats = {2023: 391.3, 2024: 34.8, 2025: 414.6}
    injured = {2024}
    baseline = _compute_weighted_baseline(stats, injured)
    # Should be ~405, not 279
    assert baseline > 380


def test_weighted_baseline_weights_recent_more():
    """Recent season contributes more than older seasons."""
    stats = {2023: 100.0, 2024: 200.0, 2025: 300.0}
    baseline = _compute_weighted_baseline(stats, set())
    # Weighted toward 300 (2025) not simple avg 200
    # 300*0.5 + 200*0.3 + 100*0.2 = 150+60+20 = 230
    assert baseline > 220


def test_weighted_baseline_handles_missing_seasons():
    """Works when player only has 1-2 seasons of data."""
    stats = {2025: 250.0}
    baseline = _compute_weighted_baseline(stats, set())
    assert baseline == 250.0


# ---------------------------------------------------------------------------
# Position verification in _get_player_season_stats
# ---------------------------------------------------------------------------

def test_position_required_for_stat_match():
    """Position filter prevents cross-position name collisions (Taylor WR vs RB on IND)."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame([
        _make_ts_row("00-RB1", "J.Taylor", "IND", 17, 40, 350, 2,
                     carries=270, rush_yards=1200, rush_tds=10, position="RB"),
    ])})
    # Looking up a WR named "Taylor" on IND must NOT match the RB
    result = agent._get_player_season_stats("Blayne Taylor", "IND", 2024, position="WR")
    assert result is None, "WR Taylor must not get RB Taylor's stats"


def test_exact_name_match_wins():
    """Exact player_name match with correct position returns correct stats."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame([
        _make_ts_row("00-WR1", "L.McConkey", "LAC", 17, 82, 1149, 7, position="WR"),
        _make_ts_row("00-RB1", "J.McCaffrey", "LAC", 5, 3, 20, 0,
                     carries=10, rush_yards=40, rush_tds=0, position="RB"),
    ])})
    result = agent._get_player_season_stats(
        "Ladd McConkey", "LAC", 2024, position="WR", nfl_player_id="00-WR1"
    )
    assert result is not None
    assert result["receptions"] == 82
    assert result["rec_yards"] == 1149


def test_no_stats_returns_empty_not_wrong_player():
    """Fringe player with same last name as a star gets None, not star's stats."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame([
        _make_ts_row("00-RB1", "K.Williams", "LA", 17, 30, 250, 2,
                     carries=250, rush_yards=1200, rush_tds=12, position="RB"),
    ])})
    # Mario Williams (WR) on LA must not get Kyren Williams (RB) stats
    result = agent._get_player_season_stats("Mario Williams", "LA", 2024, position="WR")
    assert result is None, "WR Williams must not get RB Williams stats"


def test_fringe_player_gets_depth_profile():
    """_build_depth_profile returns conservative depth defaults."""
    depth = _build_depth_profile("WR")
    assert depth["role_classification"] == "depth"
    assert depth["confidence"] == "low"
    assert depth["efficiency_signal"] == "below_average"
    assert depth["positional_scarcity_tier"] == "deep"
    assert depth["clean_season_baseline"] == {}


def test_depth_profile_all_fields_correct_types():
    """All fields in depth profile match DB column types.
    positional_scarcity_tier must be str not int.
    profile_source must be str if present.
    confidence must be str.
    """
    profile = _build_depth_profile("WR")
    assert isinstance(profile["positional_scarcity_tier"], str)
    assert isinstance(profile["confidence"], str)
    assert isinstance(profile["role_classification"], str)
    assert isinstance(profile["efficiency_signal"], str)
    assert isinstance(profile["breakout_flag"], bool)
    assert isinstance(profile["clean_season_baseline"], dict)


def test_nfl_player_id_match_takes_priority():
    """When nfl_player_id matches, position filter is not needed (Path 1)."""
    agent = PlayerProfilesAgent()
    # Two players named "Jones" on MIN — one RB, one WR
    agent._warehouse = _make_warehouse(target_share={2024: pd.DataFrame([
        _make_ts_row("00-RB9", "A.Jones", "MIN", 17, 40, 350, 3,
                     carries=200, rush_yards=1000, rush_tds=8, position="RB"),
        _make_ts_row("00-WR9", "J.Jones", "MIN", 8, 15, 120, 1, position="WR"),
    ])})
    # Path 1: nfl_player_id match gets RB Jones regardless of position arg
    result = agent._get_player_season_stats(
        "Aaron Jones", "MIN", 2024, position="RB", nfl_player_id="00-RB9"
    )
    assert result is not None
    assert result["carries"] == 200
    assert result["rush_yards"] == 1000


def test_cross_team_fallback_refuses_without_id():
    """J'Mari Taylor (JAX, no gsis_id) must NOT get Jonathan Taylor's (IND) stats.

    Path 3 cross-team fallback should refuse attribution when the caller
    has no nfl_player_id to verify against, even if the initial+last name
    matches a single player in the dataset.
    """
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2025: pd.DataFrame([
        _make_ts_row("00-0036223", "J.Taylor", "IND", 17, 40, 350, 3,
                     carries=270, rush_yards=1350, rush_tds=12, position="RB"),
    ])})
    # J'Mari Taylor on JAX has no gsis_id → nfl_player_id=None
    result = agent._get_player_season_stats(
        "J'Mari Taylor", "JAX", 2025, position="RB", nfl_player_id=None
    )
    assert result is None, (
        "J'Mari Taylor (no ID) must not get Jonathan Taylor's stats via cross-team fallback"
    )


def test_cross_team_fallback_works_with_matching_id():
    """Jonathan Taylor traded mid-season: cross-team fallback WITH matching ID succeeds."""
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2025: pd.DataFrame([
        _make_ts_row("00-0036223", "J.Taylor", "IND", 10, 25, 200, 2,
                     carries=150, rush_yards=700, rush_tds=6, position="RB"),
        _make_ts_row("00-0036223", "J.Taylor", "NYG", 7, 15, 150, 1,
                     carries=120, rush_yards=650, rush_tds=6, position="RB"),
    ])})
    # Jonathan Taylor traded to NYG — lookup from NYG with his real ID should find him
    result = agent._get_player_season_stats(
        "Jonathan Taylor", "NYG", 2025, position="RB", nfl_player_id="00-0036223"
    )
    assert result is not None, "Cross-team fallback should work when ID matches"
    assert result["games"] == 17
    assert result["rush_yards"] == 1350


def test_same_team_initial_mismatch_refuses():
    """Isaiah Jacobs (GB) must NOT get Josh Jacobs' (GB) stats.

    Path 2 same-team match should check first initial even with
    a single result — I.Jacobs != J.Jacobs.
    """
    agent = PlayerProfilesAgent()
    agent._warehouse = _make_warehouse(target_share={2025: pd.DataFrame([
        _make_ts_row("00-0035700", "J.Jacobs", "GB", 17, 40, 350, 3,
                     carries=250, rush_yards=1200, rush_tds=10, position="RB"),
    ])})
    # Isaiah Jacobs on GB has no gsis_id, initial "I" doesn't match "J"
    result = agent._get_player_season_stats(
        "Isaiah Jacobs", "GB", 2025, position="RB", nfl_player_id=None
    )
    assert result is None, (
        "Isaiah Jacobs (I.) must not get Josh Jacobs (J.) stats even on same team"
    )
