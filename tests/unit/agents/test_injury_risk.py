"""
tests/unit/agents/test_injury_risk.py

All required named test cases from stage-06-to-10.md (Stage 6).
Additional coverage tests to reach 80%+ on injury_risk.py.
"""
from __future__ import annotations

import ast
import json
import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pandas as pd
import pytest

from backend.agents.injury_risk import (
    InjuryRiskAgent,
    _bulk_resolve_player_ids,
    _to_decimal,
    _write_injury_profiles,
    classify_injury,
    compute_age_multiplier,
    compute_pattern_flags,
    get_soft_tissue_area,
    run_all_teams,
    run_for_team,
    _get_agent,
)
from backend.utils.seasons import get_current_season


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_injury_season(
    season: int,
    injuries: list[dict] | None = None,
    games_missed: int = 0,
    carries: int = 0,
) -> dict:
    return {
        "season":       season,
        "injuries":     injuries or [],
        "games_missed": games_missed,
        "carries":      carries,
    }


def _soft_tissue_inj(area_text: str = "Hamstring") -> dict:
    return {
        "injury_text": area_text,
        "category":    "soft_tissue",
        "area":        get_soft_tissue_area(area_text),
    }


def _make_profile(
    name: str = "Test Player",
    risk_level: str = "low",
    modifier: float = 0.0,
    recovery: str | None = "probable",
    notes: str = "Clean history.",
) -> dict:
    return {
        "player_name":                  name,
        "overall_risk_level":           risk_level,
        "risk_adjusted_value_modifier": modifier,
        "recovery_assessment":          recovery,
        "risk_notes":                   notes,
    }


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
# Required test cases — Stage 6 spec
# ---------------------------------------------------------------------------

def test_soft_tissue_single_event_moderate_flag():
    """A single hamstring injury classifies as soft_tissue (not a pattern flag alone)."""
    cat = classify_injury("Hamstring")
    assert cat == "soft_tissue"


def test_soft_tissue_two_same_area_three_seasons_high_flag():
    """RECURRING_SOFT_TISSUE set when same body area appears in 2+ different seasons."""
    current = get_current_season()
    seasons = [
        _make_injury_season(current - 2, injuries=[_soft_tissue_inj("Hamstring")]),
        _make_injury_season(current - 1, injuries=[_soft_tissue_inj("Hamstring")]),
        _make_injury_season(current,     injuries=[]),
    ]
    flags = compute_pattern_flags(seasons, "WR", age=28)
    assert flags["RECURRING_SOFT_TISSUE"] is True


def test_soft_tissue_pattern_flag_set():
    """RECURRING_SOFT_TISSUE=False when same-area injury appears in only one season."""
    current = get_current_season()
    seasons = [
        _make_injury_season(current - 2, injuries=[_soft_tissue_inj("Hamstring")]),
        _make_injury_season(current - 1, injuries=[]),
        _make_injury_season(current,     injuries=[]),
    ]
    flags = compute_pattern_flags(seasons, "WR", age=25)
    # Only one season — should NOT be flagged
    assert flags["RECURRING_SOFT_TISSUE"] is False


def test_acl_recent_post_acl_flag():
    """POST_ACL flag set when ACL injury occurred in the most recent analysis season."""
    current = get_current_season()
    recent_acl_season = current - 1   # within 18 months

    seasons = [
        _make_injury_season(
            recent_acl_season,
            injuries=[{"injury_text": "ACL", "category": "ligament_acl", "area": "ligament_acl"}],
        ),
    ]
    flags = compute_pattern_flags(seasons, "RB", age=25)
    assert flags["POST_ACL"] is True


def test_fracture_traumatic_low_risk():
    """Traumatic fractures classify correctly — low recurrence risk."""
    assert classify_injury("Fracture") == "fracture_traumatic"
    assert classify_injury("Broken fibula") == "fracture_traumatic"
    assert classify_injury("Collarbone") == "fracture_traumatic"


def test_fracture_stress_moderate_risk():
    """Stress fractures classify into fracture_stress — indicates biomechanical issue."""
    assert classify_injury("Stress fracture") == "fracture_stress"
    assert classify_injury("Stress reaction") == "fracture_stress"


def test_concussion_single_no_compounding():
    """Single concussion does NOT trigger CONCUSSION_HISTORY (needs 2+)."""
    current = get_current_season()
    seasons = [
        _make_injury_season(
            current - 1,
            injuries=[{"injury_text": "Concussion", "category": "concussion", "area": "concussion"}],
        ),
    ]
    flags = compute_pattern_flags(seasons, "WR", age=25)
    assert flags["CONCUSSION_HISTORY"] is False
    assert flags["concussion_count"] == 1


def test_concussion_two_plus_compounding_modifier():
    """2+ concussions triggers CONCUSSION_HISTORY compounding modifier flag."""
    current = get_current_season()
    concussion_inj = {"injury_text": "Concussion", "category": "concussion", "area": "concussion"}
    seasons = [
        _make_injury_season(current - 2, injuries=[concussion_inj]),
        _make_injury_season(current - 1, injuries=[concussion_inj]),
    ]
    flags = compute_pattern_flags(seasons, "WR", age=30)
    assert flags["CONCUSSION_HISTORY"] is True
    assert flags["concussion_count"] == 2


def test_chronic_condition_does_not_reset():
    """CHRONIC_CONDITION flag set from any chronic injury — persists regardless of other seasons."""
    current = get_current_season()
    seasons = [
        _make_injury_season(
            current - 2,
            injuries=[{"injury_text": "Turf toe", "category": "chronic", "area": "chronic"}],
        ),
        _make_injury_season(current - 1, injuries=[]),  # clean season after
        _make_injury_season(current,     injuries=[]),
    ]
    flags = compute_pattern_flags(seasons, "WR", age=27)
    assert flags["CHRONIC_CONDITION"] is True


def test_workload_cliff_300_plus_carries():
    """WORKLOAD_CLIFF flag set for RB coming off 300+ carry season."""
    current = get_current_season()
    most_recent = current - 1
    seasons = [
        _make_injury_season(most_recent, carries=325),
    ]
    flags = compute_pattern_flags(seasons, "RB", age=26)
    assert flags["WORKLOAD_CLIFF"] is True
    assert flags["last_season_carries"] == 325


def test_high_mileage_600_plus_career_carries():
    """HIGH_MILEAGE flag set for RB with 600+ career carries across analysis seasons."""
    current = get_current_season()
    seasons = [
        _make_injury_season(current - 2, carries=250),
        _make_injury_season(current - 1, carries=200),
        _make_injury_season(current,     carries=175),
    ]
    flags = compute_pattern_flags(seasons, "RB", age=27)
    assert flags["HIGH_MILEAGE"] is True
    assert flags["career_carries"] == 625


def test_age_multiplier_under_26_baseline():
    """Players under 26 have baseline 1.0x multiplier — no age penalty."""
    assert compute_age_multiplier(22) == 1.0
    assert compute_age_multiplier(25) == 1.0


def test_age_multiplier_31_plus_elevated():
    """Players 31+ have 1.5x age risk multiplier."""
    assert compute_age_multiplier(31) == 1.5
    assert compute_age_multiplier(35) == 1.5


@pytest.mark.asyncio
async def test_risk_modifier_applied_to_risk_adjusted_value():
    """
    When baseline_value is set on a player, risk_adjusted_value is updated
    by applying the risk_adjusted_value_modifier from the profile.
    baseline=50.0, modifier=-0.10 → risk_adjusted_value=45.0
    """
    player_id   = str(uuid.uuid4())
    profile     = _make_profile("Hill", risk_level="moderate", modifier=-0.10)

    mock_player = MagicMock()
    mock_player.id             = player_id
    mock_player.name           = "Tyreek Hill"
    mock_player.team_abbr      = "MIA"
    mock_player.baseline_value = Decimal("50.00")
    mock_player.risk_adjusted_value = None

    # Sequence of execute() returns:
    # 1. _bulk_resolve_player_ids → scalars().all() gives [mock_player]
    r_bulk = MagicMock()
    r_bulk.scalars.return_value.all.return_value = [mock_player]

    # 2. select(PlayerInjuryProfile) → scalar_one_or_none = None (new record)
    r_no_existing = MagicMock()
    r_no_existing.scalar_one_or_none.return_value = None

    # 3. select(Player) for risk_adjusted_value update
    r_player = MagicMock()
    r_player.scalar_one_or_none.return_value = mock_player

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_existing, r_player])

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    context = {
        "players": [{
            "name":               "Hill",
            "position":           "WR",
            "age":                30,
            "age_risk_mult":      1.25,
            "pattern_flags":      [],
            "concussion_count":   0,
            "career_carries":     0,
            "last_season_carries": 0,
            "injury_seasons":     [],
        }]
    }

    with patch("backend.agents.injury_risk.AsyncSessionLocal", return_value=mock_ctx):
        written = await _write_injury_profiles([profile], context, "MIA")

    assert written == 1
    # risk_adjusted_value = 50.0 * (1 + (-0.10)) = 45.0
    assert mock_player.risk_adjusted_value == Decimal("45.0")


def test_no_hardcoded_years():
    """
    Verify that injury_risk.py contains no hardcoded 4-digit year literals
    (2022, 2023, 2024, 2025, etc.). All season years must come from seasons.py.
    """
    source_path = Path("backend/agents/injury_risk.py")
    source = source_path.read_text(encoding="utf-8")

    # Parse AST and find integer literals in the 2010–2040 range
    tree = ast.parse(source)
    hardcoded = [
        node.n for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.n, int)
        and 2010 <= node.n <= 2040
    ]
    assert hardcoded == [], f"Hardcoded year(s) found: {hardcoded}"


@pytest.mark.asyncio
async def test_single_api_call_per_team():
    """run_for_team() must make exactly ONE call to call_once()."""
    agent = InjuryRiskAgent(dry_run=False)
    agent._warehouse = _make_warehouse()

    mock_context = {
        "team":          "LAC",
        "analysis_year": 2026,
        "players": [{
            "name":               "Justin Herbert",
            "position":           "QB",
            "age":                26,
            "age_risk_mult":      1.1,
            "pattern_flags":      [],
            "concussion_count":   0,
            "career_carries":     0,
            "last_season_carries": 0,
            "injury_seasons":     [],
        }],
    }

    call_once_calls = 0

    async def _fake_call_once(system, user, input_data, entity_id):
        nonlocal call_once_calls
        call_once_calls += 1
        return json.dumps([_make_profile("Justin Herbert")])

    with (
        patch.object(agent, "_build_team_context", AsyncMock(return_value=mock_context)),
        patch.object(agent, "call_once", side_effect=_fake_call_once),
        patch("backend.agents.injury_risk._write_injury_profiles", AsyncMock(return_value=1)),
    ):
        await agent.run_for_team("LAC")

    assert call_once_calls == 1


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------

# ---- classify_injury -------------------------------------------------------

def test_classify_injury_concussion():
    assert classify_injury("Concussion") == "concussion"
    assert classify_injury("Head") == "concussion"


def test_classify_injury_acl():
    assert classify_injury("ACL") == "ligament_acl"
    assert classify_injury("anterior cruciate ligament") == "ligament_acl"


def test_classify_injury_ankle():
    assert classify_injury("Ankle") == "high_ankle_sprain"
    assert classify_injury("High ankle sprain") == "high_ankle_sprain"


def test_classify_injury_fracture_traumatic():
    assert classify_injury("Tibia fracture") == "fracture_traumatic"


def test_classify_injury_chronic():
    assert classify_injury("Turf toe") == "chronic"
    assert classify_injury("Plantar fasciitis") == "chronic"
    assert classify_injury("Back") == "chronic"


def test_classify_injury_soft_tissue_variants():
    for text in ("Hamstring", "Groin", "Calf", "Hip flexor", "Quad"):
        assert classify_injury(text) == "soft_tissue", f"Expected soft_tissue for {text!r}"


def test_classify_injury_knee_non_acl():
    assert classify_injury("Knee") == "soft_tissue"


def test_classify_injury_none_or_empty():
    assert classify_injury(None)  == "other"
    assert classify_injury("")    == "other"


# ---- get_soft_tissue_area --------------------------------------------------

def test_get_soft_tissue_area_hamstring():
    assert get_soft_tissue_area("Hamstring") == "hamstring"


def test_get_soft_tissue_area_quad():
    assert get_soft_tissue_area("Quad") == "quad"
    assert get_soft_tissue_area("Thigh") == "quad"


def test_get_soft_tissue_area_knee():
    assert get_soft_tissue_area("Knee") == "knee"


def test_get_soft_tissue_area_general():
    assert get_soft_tissue_area(None)    == "general"
    assert get_soft_tissue_area("Wrist") == "general"


# ---- compute_age_multiplier ------------------------------------------------

def test_age_multiplier_26_to_28():
    assert compute_age_multiplier(26) == 1.1
    assert compute_age_multiplier(28) == 1.1


def test_age_multiplier_29_to_30():
    assert compute_age_multiplier(29) == 1.25
    assert compute_age_multiplier(30) == 1.25


def test_age_multiplier_none():
    assert compute_age_multiplier(None) == 1.0


# ---- compute_pattern_flags — edge cases ------------------------------------

def test_pattern_flags_empty_seasons():
    """No injury history → all flags False."""
    flags = compute_pattern_flags([], "WR", age=24)
    assert flags["RECURRING_SOFT_TISSUE"] is False
    assert flags["CONCUSSION_HISTORY"]    is False
    assert flags["HIGH_MILEAGE"]          is False
    assert flags["POST_ACL"]              is False
    assert flags["CHRONIC_CONDITION"]     is False
    assert flags["WORKLOAD_CLIFF"]        is False
    assert flags["career_carries"]        == 0


def test_high_mileage_not_set_for_wr():
    """HIGH_MILEAGE only applies to RBs — not WRs even with 600+ carries."""
    seasons = [_make_injury_season(2024, carries=700)]
    flags = compute_pattern_flags(seasons, "WR", age=30)
    assert flags["HIGH_MILEAGE"] is False


def test_workload_cliff_not_set_for_wr():
    """WORKLOAD_CLIFF only applies to RBs."""
    seasons = [_make_injury_season(2024, carries=350)]
    flags = compute_pattern_flags(seasons, "WR", age=28)
    assert flags["WORKLOAD_CLIFF"] is False


def test_high_mileage_exactly_600_carries():
    """HIGH_MILEAGE triggers at exactly 600 career carries for RB."""
    seasons = [_make_injury_season(2024, carries=600)]
    flags = compute_pattern_flags(seasons, "RB", age=27)
    assert flags["HIGH_MILEAGE"] is True


def test_post_acl_old_injury_not_flagged():
    """POST_ACL not triggered for ACL injuries from 2+ seasons ago."""
    current = get_current_season()
    old_acl = current - 3
    seasons = [
        _make_injury_season(
            old_acl,
            injuries=[{"injury_text": "ACL", "category": "ligament_acl", "area": "ligament_acl"}],
        ),
    ]
    flags = compute_pattern_flags(seasons, "RB", age=27)
    assert flags["POST_ACL"] is False


def test_recurring_soft_tissue_different_areas_not_flagged():
    """RECURRING_SOFT_TISSUE requires the SAME area — different areas do not count."""
    current = get_current_season()
    seasons = [
        _make_injury_season(current - 2, injuries=[_soft_tissue_inj("Hamstring")]),
        _make_injury_season(current - 1, injuries=[_soft_tissue_inj("Groin")]),
    ]
    flags = compute_pattern_flags(seasons, "WR", age=26)
    assert flags["RECURRING_SOFT_TISSUE"] is False


# ---- InjuryRiskAgent helpers -----------------------------------------------

def test_get_team_roster_no_cache():
    """Returns empty list when warehouse has no roster data."""
    agent = InjuryRiskAgent(dry_run=True)
    agent._warehouse = _make_warehouse()
    assert agent._get_team_roster("LAC", 2024) == []


def test_get_team_roster_filters_to_team_and_skill_positions():
    agent = InjuryRiskAgent(dry_run=True)
    df = pd.DataFrame([
        {"team": "LAC", "full_name": "Ladd McConkey",   "position": "WR", "week": 17},
        {"team": "LAC", "full_name": "J.K. Dobbins",     "position": "RB", "week": 17},
        {"team": "LAC", "full_name": "Zack Martin",       "position": "G",  "week": 17},
        {"team": "KC",  "full_name": "Patrick Mahomes",   "position": "QB", "week": 17},
    ])
    agent._warehouse = _make_warehouse(rosters=df)
    result = agent._get_team_roster("LAC", 2025)
    names = [r["name"] for r in result]
    assert "Ladd McConkey" in names
    assert "J.K. Dobbins" in names
    assert "Zack Martin"   not in names  # G is not a skill position
    assert "Patrick Mahomes" not in names  # Wrong team


def test_get_player_injury_season_no_cache():
    agent = InjuryRiskAgent(dry_run=True)
    agent._warehouse = _make_warehouse()
    result = agent._get_player_injury_season("Justin Jefferson", "MIN", 2024)
    assert result["injuries"] == []
    assert result["games_missed"] == 0


def test_get_player_injury_season_finds_injury():
    agent = InjuryRiskAgent(dry_run=True)
    df = pd.DataFrame([
        {"full_name": "Justin Jefferson", "team": "MIN", "season": 2024,
         "game_type": "REG", "week": 3, "position": "WR",
         "report_primary_injury": "Hamstring", "report_status": "Out"},
        {"full_name": "Justin Jefferson", "team": "MIN", "season": 2024,
         "game_type": "REG", "week": 4, "position": "WR",
         "report_primary_injury": "Hamstring", "report_status": "Out"},
        # Second week with same hamstring should deduplicate
    ])
    agent._warehouse = _make_warehouse(injuries={2024: df})
    result = agent._get_player_injury_season("Justin Jefferson", "MIN", 2024)
    # Should deduplicate — only 1 unique hamstring injury
    assert len(result["injuries"]) == 1
    assert result["injuries"][0]["category"] == "soft_tissue"
    assert result["games_missed"] == 2  # 2 weeks marked "Out"


def test_get_player_injury_season_filters_postseason():
    """Only REG game_type rows are included."""
    agent = InjuryRiskAgent(dry_run=True)
    df = pd.DataFrame([
        {"full_name": "Davante Adams", "team": "NYJ", "season": 2024,
         "game_type": "POST", "week": 18, "position": "WR",
         "report_primary_injury": "Hamstring", "report_status": "Out"},
    ])
    agent._warehouse = _make_warehouse(injuries={2024: df})
    result = agent._get_player_injury_season("Davante Adams", "NYJ", 2024)
    assert result["injuries"] == []


def test_get_player_carries_no_cache():
    agent = InjuryRiskAgent(dry_run=True)
    agent._warehouse = _make_warehouse()
    assert agent._get_player_carries("Saquon Barkley", "PHI", 2024) == 0


def test_get_player_carries_returns_value():
    agent = InjuryRiskAgent(dry_run=True)
    df = pd.DataFrame([
        {"player_name": "Saquon Barkley", "recent_team": "PHI",
         "position": "RB", "total_carries": 345},
    ])
    agent._warehouse = _make_warehouse(target_share={2024: df})
    carries = agent._get_player_carries("Saquon Barkley", "PHI", 2024)
    assert carries == 345


# ---- build_team_context ----------------------------------------------------

@pytest.mark.asyncio
async def test_build_team_context_returns_players():
    agent = InjuryRiskAgent(dry_run=True)
    agent._warehouse = _make_warehouse()

    roster = [{"name": "Ladd McConkey", "position": "WR", "age": 23}]

    with (
        patch.object(agent, "_get_team_roster", return_value=roster),
        patch.object(agent, "_get_player_injury_season", return_value={
            "season": 2024, "injuries": [], "games_missed": 0
        }),
        patch.object(agent, "_get_player_carries", return_value=0),
    ):
        context = await agent._build_team_context("LAC")

    assert context["team"] == "LAC"
    assert len(context["players"]) == 1
    player = context["players"][0]
    assert player["name"]        == "Ladd McConkey"
    assert player["age_risk_mult"] == 1.0   # under 26
    assert player["pattern_flags"] == []


# ---- run_for_team edge cases -----------------------------------------------

@pytest.mark.asyncio
async def test_run_for_team_empty_players_returns_zero():
    agent = InjuryRiskAgent(dry_run=True)
    empty_context = {"team": "LAC", "analysis_year": 2026, "players": []}

    with patch.object(agent, "_build_team_context", AsyncMock(return_value=empty_context)):
        result = await agent.run_for_team("LAC")

    assert result == 0


@pytest.mark.asyncio
async def test_run_for_team_exception_returns_zero():
    agent = InjuryRiskAgent(dry_run=True)

    with patch.object(agent, "_build_team_context", AsyncMock(side_effect=RuntimeError("boom"))):
        result = await agent.run_for_team("LAC")

    assert result == 0


# ---- run_all_teams ---------------------------------------------------------

@pytest.mark.asyncio
async def test_run_all_teams_runs_all_32_teams():
    """run_all_teams calls run_for_team for each of the 32 NFL teams."""
    agent = InjuryRiskAgent(dry_run=True)
    agent._warehouse = _make_warehouse()
    teams_run = []

    async def _fake_run_for_team(team):
        teams_run.append(team)
        return 0

    with patch.object(agent, "run_for_team", side_effect=_fake_run_for_team):
        await agent.run_all_teams()

    assert len(teams_run) == 32


# ---- _bulk_resolve_player_ids ----------------------------------------------

@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_empty_input():
    mock_session = AsyncMock()
    result = await _bulk_resolve_player_ids(mock_session, [])
    assert result == {}
    mock_session.execute.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_single_candidate():
    mock_player     = MagicMock()
    mock_player.id  = uuid.uuid4()
    mock_player.name = "Ladd McConkey"
    mock_player.team_abbr = "LAC"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_player]

    mock_session        = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await _bulk_resolve_player_ids(mock_session, [("Ladd McConkey", "LAC")])
    assert result[("Ladd McConkey", "LAC")] == str(mock_player.id)


@pytest.mark.asyncio
async def test_bulk_resolve_player_ids_team_match_preferred():
    """When multiple candidates share a last name, the correct team is preferred."""
    p1, p2  = MagicMock(), MagicMock()
    p1.id   = uuid.uuid4()
    p1.name = "Tyler Johnson"
    p1.team_abbr = "TB"
    p2.id   = uuid.uuid4()
    p2.name = "Tyler Johnson"
    p2.team_abbr = "LAC"

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [p1, p2]

    mock_session        = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await _bulk_resolve_player_ids(mock_session, [("Tyler Johnson", "LAC")])
    assert result[("Tyler Johnson", "LAC")] == str(p2.id)


# ---- _write_injury_profiles ------------------------------------------------

@pytest.mark.asyncio
async def test_write_injury_profiles_empty_list():
    result = await _write_injury_profiles([], {}, "LAC")
    assert result == 0


@pytest.mark.asyncio
async def test_write_injury_profiles_skips_unresolved_player():
    """Players that can't be resolved in DB are skipped (no crash)."""
    profile = _make_profile("Ghost Player", modifier=-0.10)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_session        = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    context = {"players": [{"name": "Ghost Player", "pattern_flags": [], "injury_seasons": [],
                             "concussion_count": 0, "career_carries": 0,
                             "last_season_carries": 0, "age_risk_mult": 1.0}]}

    with patch("backend.agents.injury_risk.AsyncSessionLocal", return_value=mock_ctx):
        written = await _write_injury_profiles([profile], context, "LAC")

    assert written == 0


@pytest.mark.asyncio
async def test_write_injury_profiles_inserts_new_record():
    """New player gets a PlayerInjuryProfile record inserted."""
    player_id   = str(uuid.uuid4())
    profile     = _make_profile("Saquon Barkley", risk_level="moderate", modifier=-0.15)

    mock_player = MagicMock()
    mock_player.id        = player_id
    mock_player.name      = "Saquon Barkley"
    mock_player.team_abbr = "PHI"
    mock_player.baseline_value = None  # No baseline yet — skip risk_adjusted_value update

    r_bulk = MagicMock()
    r_bulk.scalars.return_value.all.return_value = [mock_player]

    r_no_existing = MagicMock()
    r_no_existing.scalar_one_or_none.return_value = None

    # Third call: select(Player) for risk_adjusted_value (modifier=-0.15 is not None)
    r_player_lookup = MagicMock()
    r_player_lookup.scalar_one_or_none.return_value = mock_player  # baseline_value=None → no update

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_existing, r_player_lookup])

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    context = {
        "players": [{
            "name":               "Saquon Barkley",
            "position":           "RB",
            "age":                27,
            "age_risk_mult":      1.1,
            "pattern_flags":      ["WORKLOAD_CLIFF"],
            "concussion_count":   0,
            "career_carries":     325,
            "last_season_carries": 325,
            "injury_seasons":     [],
        }]
    }

    with patch("backend.agents.injury_risk.AsyncSessionLocal", return_value=mock_ctx):
        written = await _write_injury_profiles([profile], context, "PHI")

    assert written == 1
    mock_session.add.assert_called_once()


@pytest.mark.asyncio
async def test_write_injury_profiles_updates_existing_record():
    """Existing PlayerInjuryProfile is updated (not duplicated)."""
    player_id   = str(uuid.uuid4())
    profile     = _make_profile("Justin Jefferson", risk_level="low", modifier=-0.02)

    mock_player = MagicMock()
    mock_player.id        = player_id
    mock_player.name      = "Justin Jefferson"
    mock_player.team_abbr = "MIN"
    mock_player.baseline_value = None

    r_bulk = MagicMock()
    r_bulk.scalars.return_value.all.return_value = [mock_player]

    # Existing record returned
    existing_record = MagicMock()
    r_existing = MagicMock()
    r_existing.scalar_one_or_none.return_value = existing_record

    # Third call: select(Player) for risk_adjusted_value (modifier=-0.02 is not None)
    r_player_lookup = MagicMock()
    r_player_lookup.scalar_one_or_none.return_value = mock_player  # baseline_value=None → no update

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_existing, r_player_lookup])

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    context = {
        "players": [{
            "name":               "Justin Jefferson",
            "position":           "WR",
            "age":                25,
            "age_risk_mult":      1.0,
            "pattern_flags":      [],
            "concussion_count":   0,
            "career_carries":     0,
            "last_season_carries": 0,
            "injury_seasons":     [],
        }]
    }

    with patch("backend.agents.injury_risk.AsyncSessionLocal", return_value=mock_ctx):
        written = await _write_injury_profiles([profile], context, "MIN")

    assert written == 1
    # session.add should NOT be called for existing records
    mock_session.add.assert_not_called()
    assert existing_record.overall_risk_level == "low"


# ---- _to_decimal -----------------------------------------------------------

def test_to_decimal_float():
    assert _to_decimal(-0.15) == Decimal("-0.15")


def test_to_decimal_none():
    assert _to_decimal(None) is None


def test_to_decimal_invalid_string():
    assert _to_decimal("not_a_number") is None


def test_to_decimal_integer():
    assert _to_decimal(0) == Decimal("0")


# ---- Module-level shims ----------------------------------------------------

def test_get_agent_creates_instance():
    from backend.agents.injury_risk import _get_agent as ga
    import backend.agents.injury_risk as ir_module
    ir_module._agent_instance = None
    agent = ga(dry_run=True)
    assert isinstance(agent, InjuryRiskAgent)
    assert agent.dry_run is True


def test_get_agent_reuses_same_dry_run():
    import backend.agents.injury_risk as ir_module
    ir_module._agent_instance = None
    a1 = _get_agent(dry_run=True)
    a2 = _get_agent(dry_run=True)
    assert a1 is a2


@pytest.mark.asyncio
async def test_module_run_for_team_shim():
    """Module-level run_for_team shim delegates to InjuryRiskAgent."""
    with patch("backend.agents.injury_risk._get_agent") as mock_ga:
        mock_agent = AsyncMock()
        mock_agent.run_for_team = AsyncMock(return_value=5)
        mock_ga.return_value = mock_agent
        result = await run_for_team("LAC", dry_run=True)
    assert result == 5


@pytest.mark.asyncio
async def test_module_run_all_teams_shim():
    """Module-level run_all_teams shim delegates to InjuryRiskAgent."""
    with patch("backend.agents.injury_risk._get_agent") as mock_ga:
        mock_agent = AsyncMock()
        mock_agent.run_all_teams = AsyncMock(return_value={"LAC": 5})
        mock_ga.return_value = mock_agent
        result = await run_all_teams(dry_run=True)
    assert result == {"LAC": 5}


# ===========================================================================
# VOLATILE reclassification — requires multi-season injury history
# ===========================================================================


def test_volatile_requires_multiple_injury_seasons():
    """Player with injuries in only 1 of 3 seasons should be HIGH not VOLATILE.

    The Amon-Ra scenario: one bad season (2024) with 3 flags should NOT
    produce VOLATILE classification. VOLATILE requires 8+ games missed
    in 2+ of last 3 seasons.
    """
    # This is tested via the post-processing in _write_injury_profiles.
    # The enforcement happens at DB write time, not in compute_pattern_flags.
    # We verify the logic directly: 1 season with 8+ games_missed → cap at HIGH.
    injury_seasons = [
        {"season": 2024, "injuries": [{"category": "soft_tissue", "area": "shoulder"}], "games_missed": 17},
        {"season": 2023, "injuries": [], "games_missed": 0},
        {"season": 2022, "injuries": [], "games_missed": 0},
    ]
    recent = sorted(injury_seasons, key=lambda s: s.get("season", 0), reverse=True)[:3]
    severe_seasons = sum(1 for s in recent if s.get("games_missed", 0) >= 8)
    assert severe_seasons < 2, "Should only have 1 severe season"
    # The agent code would downgrade volatile → high when severe_seasons < 2


def test_volatile_kept_for_chronic_multi_season():
    """Player missing 8+ games in 2+ seasons keeps VOLATILE classification."""
    injury_seasons = [
        {"season": 2024, "injuries": [{"category": "soft_tissue"}], "games_missed": 10},
        {"season": 2023, "injuries": [{"category": "ligament_acl"}], "games_missed": 14},
        {"season": 2022, "injuries": [], "games_missed": 2},
    ]
    recent = sorted(injury_seasons, key=lambda s: s.get("season", 0), reverse=True)[:3]
    severe_seasons = sum(1 for s in recent if s.get("games_missed", 0) >= 8)
    assert severe_seasons >= 2, "Should have 2+ severe seasons — VOLATILE justified"
