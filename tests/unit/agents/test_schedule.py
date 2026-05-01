"""
tests/unit/agents/test_schedule.py

All required named test cases from stage-06-to-10.md (Stage 7).
Additional coverage tests to reach 80%+ on schedule.py.
"""
from __future__ import annotations

import ast
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from backend.agents.schedule import (
    ScheduleAgent,
    _bulk_resolve_player_ids,
    _get_agent,
    _to_decimal,
    _write_schedules,
    compute_def_grades,
    is_weather_risk_game,
    lookup_def_grade,
    run_all_teams,
    run_for_team,
    EARLY_WINDOW,
    PLAYOFF_WINDOW,
    WEATHER_RISK_TEAMS,
    FAVORABLE_RANK_CUTOFF,
    TOUGH_RANK_CUTOFF,
)


# ---------------------------------------------------------------------------
# Autouse fixture: clear ClassVar cache between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_schedule_cache():
    ScheduleAgent._data_cache.clear()
    yield
    ScheduleAgent._data_cache.clear()


# ---------------------------------------------------------------------------
# DataFrame helpers
# ---------------------------------------------------------------------------

def _make_schedule_df(rows: list[dict]) -> pd.DataFrame:
    """Create a schedule DataFrame with sensible defaults."""
    defaults = {"game_type": "REG", "div_game": 0, "roof": "outdoors"}
    return pd.DataFrame([{**defaults, **r} for r in rows])


def _make_weekly_df(records: list[tuple]) -> pd.DataFrame:
    """Tuple order: (season_type, position, opponent_team, week, fantasy_points_ppr)."""
    return pd.DataFrame(
        records,
        columns=["season_type", "position", "opponent_team", "week", "fantasy_points_ppr"],
    )


def _make_player(name: str = "Justin Jefferson", team: str = "MIN", pos: str = "WR") -> MagicMock:
    p = MagicMock()
    p.id = "test-player-id"
    p.name = name
    p.team_abbr = team
    return p


def _pos_block(grade: str = "favorable") -> dict:
    return {
        "early_window_grade":           grade,
        "early_window_favorable_weeks": [1, 2, 3] if grade == "favorable" else [],
        "early_window_tough_weeks":     [] if grade == "favorable" else [1, 2, 3],
        "early_window_summary":         f"Schedule is {grade} early.",
        "full_season_grade":            grade,
        "playoff_window_grade":         grade,
        "playoff_weeks":                [14, 15, 16, 17],
        "playoff_matchups":             [{"week": 14, "opponent": "DAL", "grade": grade}],
        "playoff_summary":              f"Playoff window is {grade}.",
        "weather_risk":                 "low",
        "weather_affected_weeks":       [],
        "divisional_game_weeks":        [5, 10],
        "schedule_score":               8.5 if grade == "favorable" else 5.0,
        "schedule_notes":               f"Overall {grade} schedule.",
    }


def _make_result(
    bye_week: int = 9,
    wr_grade: str = "favorable",
    rb_grade: str = "neutral",
    te_grade: str = "tough",
) -> dict:
    return {
        "bye_week": bye_week,
        "WR": _pos_block(wr_grade),
        "RB": _pos_block(rb_grade),
        "TE": _pos_block(te_grade),
    }


def _lac_schedule_df() -> pd.DataFrame:
    """LAC plays weeks 1-17 except week 9 (bye)."""
    rows = []
    for wk in range(1, 18):
        if wk == 9:
            continue
        rows.append({
            "home_team": "LAC" if wk % 2 == 0 else "OPP",
            "away_team": "OPP" if wk % 2 == 0 else "LAC",
            "week": wk,
            "game_type": "REG",
            "div_game": 0,
            "roof": "outdoors",
        })
    return _make_schedule_df(rows)


# ---------------------------------------------------------------------------
# Required test cases — Stage 7 spec
# ---------------------------------------------------------------------------

def test_playoff_grade_is_first_class_column_not_notes():
    """playoff_window_grade must be a first-class column on PlayerSchedule, not buried in notes."""
    from backend.models.player import PlayerSchedule

    assert hasattr(PlayerSchedule, "playoff_window_grade"), (
        "PlayerSchedule.playoff_window_grade is not a first-class column — it must not be buried "
        "in a text notes field"
    )
    # Confirm it's a mapped column (not just a Python attribute)
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(PlayerSchedule)
    col_names = [c.key for c in mapper.mapper.column_attrs]
    assert "playoff_window_grade" in col_names, (
        "playoff_window_grade must be a mapped DB column, not a plain attribute"
    )


def test_early_window_correct_weeks_1_to_6():
    assert EARLY_WINDOW == {1, 2, 3, 4, 5, 6}
    assert 1 in EARLY_WINDOW
    assert 6 in EARLY_WINDOW
    assert 7 not in EARLY_WINDOW
    assert 0 not in EARLY_WINDOW


def test_playoff_window_correct_weeks_14_to_17():
    assert PLAYOFF_WINDOW == {14, 15, 16, 17}
    assert 14 in PLAYOFF_WINDOW
    assert 17 in PLAYOFF_WINDOW
    assert 13 not in PLAYOFF_WINDOW
    assert 18 not in PLAYOFF_WINDOW


def test_defensive_grade_adjusted_for_fa_departure():
    """
    Simulates a corner FA departure: NYJ now allows lots of PPR to WRs (rank 1 = favorable).
    The inversion logic underpins how FA losses cascade to position-group schedule grades.
    """
    records = []
    # NYJ: weak post-FA (lost top corner) — allows 50 PPR/game vs WR
    for week in range(1, 5):
        records.append(("REG", "WR", "NYJ", week, 50.0))
    # CHI: strong run defense — allows only 10 PPR/game vs WR
    for week in range(1, 5):
        records.append(("REG", "WR", "CHI", week, 10.0))
    # 30 other teams at ~25 PPR/game to pad the field to near-32
    for i in range(30):
        records.append(("REG", "WR", f"T{i:02d}", 1, 25.0))

    grades = compute_def_grades(_make_weekly_df(records))

    nyj_wr = grades[(grades["defense_team"] == "NYJ") & (grades["position"] == "WR")]
    chi_wr = grades[(grades["defense_team"] == "CHI") & (grades["position"] == "WR")]

    assert not nyj_wr.empty
    assert nyj_wr.iloc[0]["rank"] == 1, "NYJ (most allowed) should be rank 1"
    assert nyj_wr.iloc[0]["grade"] == "favorable", "Rank 1 defense = favorable for WRs"

    assert not chi_wr.empty
    assert chi_wr.iloc[0]["grade"] == "tough", "CHI (fewest allowed) should be tough"


def test_weather_flag_outdoor_cold_city_november():
    """BUF outdoor game week 11 is weather risk; DAL same week is not."""
    # All cold cities, late season, outdoors → risk
    assert is_weather_risk_game("BUF", 11, "outdoors") is True
    assert is_weather_risk_game("GB",  14, "outdoors") is True
    assert is_weather_risk_game("CHI", 12, "outdoors") is True
    assert is_weather_risk_game("NE",  10, "outdoors") is True
    assert is_weather_risk_game("CLE", 16, "outdoors") is True
    assert is_weather_risk_game("PIT", 17, "outdoors") is True
    # Warm city → no risk
    assert is_weather_risk_game("DAL", 11, "outdoors") is False
    assert is_weather_risk_game("MIA", 14, "outdoors") is False
    # Cold city but too early (week 9)
    assert is_weather_risk_game("BUF", 9,  "outdoors") is False
    # Cold city late but dome
    assert is_weather_risk_game("BUF", 11, "dome")   is False
    assert is_weather_risk_game("BUF", 11, "closed") is False


def test_bye_week_stored_correctly():
    """_get_bye_week identifies the missing week as the bye week."""
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["schedule_df"] = _lac_schedule_df()

    assert agent._get_bye_week("LAC") == 9


async def test_position_specific_grades_stored_separately():
    """WR and RB players receive their respective position-group grades."""
    result  = _make_result(wr_grade="favorable", rb_grade="tough")
    context = {
        "players": [
            {"name": "Justin Jefferson", "position": "WR"},
            {"name": "Dalvin Cook",       "position": "RB"},
        ],
        "bye_week": 9,
    }

    p_wr = _make_player("Justin Jefferson", "MIN", "WR")
    p_wr.id   = "wr-id"
    p_wr.name = "Justin Jefferson"

    p_rb = _make_player("Dalvin Cook", "MIN", "RB")
    p_rb.id   = "rb-id"
    p_rb.name = "Dalvin Cook"

    r_bulk        = MagicMock(); r_bulk.scalars.return_value.all.return_value = [p_wr, p_rb]
    r_no_exist_wr = MagicMock(); r_no_exist_wr.scalars.return_value.first.return_value = None
    r_no_exist_rb = MagicMock(); r_no_exist_rb.scalars.return_value.first.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_exist_wr, r_no_exist_rb])

    added: list = []
    mock_session.add = MagicMock(side_effect=added.append)  # sync mock — session.add is not awaited

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.schedule.AsyncSessionLocal", return_value=mock_ctx), \
         patch("backend.agents.schedule.get_analysis_year", return_value=2026):
        written = await _write_schedules(result, context, "MIN")

    assert written == 2
    assert added[0].early_window_grade == "favorable"
    assert added[1].early_window_grade == "tough"


def test_no_hardcoded_years():
    """schedule.py must not contain literal year constants (integers 2000-2099)."""
    source = Path("backend/agents/schedule.py").read_text()
    tree   = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if 2000 <= node.value <= 2099:
                pytest.fail(
                    f"Hardcoded year {node.value} found at line {node.lineno} in schedule.py"
                )


async def test_single_api_call_per_team():
    """run_for_team must make exactly ONE call_once() call."""
    agent = ScheduleAgent(dry_run=False)

    agent._data_cache["schedule_df"] = _lac_schedule_df()
    agent._data_cache["def_grades"]  = pd.DataFrame(
        columns=["defense_team", "position", "ppr_per_game", "rank", "grade"]
    )
    agent._data_cache["rosters_2025"] = pd.DataFrame(
        {"full_name": ["Justin Herbert"], "team": ["LAC"], "position": ["QB"]}
    )

    with patch.object(agent, "call_once", new_callable=AsyncMock,
                      return_value=json.dumps(_make_result())) as mock_call, \
         patch.object(agent, "_get_team_system", new_callable=AsyncMock, return_value={}), \
         patch("backend.agents.schedule._write_schedules", new_callable=AsyncMock, return_value=1), \
         patch("backend.agents.schedule.get_current_season", return_value=2025), \
         patch("backend.agents.schedule.get_analysis_year",  return_value=2026):
        await agent.run_for_team("LAC")

    assert mock_call.call_count == 1


# ---------------------------------------------------------------------------
# compute_def_grades — additional coverage
# ---------------------------------------------------------------------------

def test_compute_def_grades_empty_df():
    result = compute_def_grades(pd.DataFrame())
    assert result.empty
    assert list(result.columns) == ["defense_team", "position", "ppr_per_game", "rank", "grade"]


def test_compute_def_grades_ranking_inversion():
    """Rank 1 = most PPR allowed. Verify descending rank assignment."""
    records = [
        ("REG", "WR", "BAD",  1, 40.0),
        ("REG", "WR", "MID",  1, 25.0),
        ("REG", "WR", "GOOD", 1, 10.0),
    ]
    grades = compute_def_grades(_make_weekly_df(records))
    bad_rank  = grades[grades["defense_team"] == "BAD"].iloc[0]["rank"]
    good_rank = grades[grades["defense_team"] == "GOOD"].iloc[0]["rank"]
    assert bad_rank < good_rank


def test_compute_def_grades_grade_thresholds():
    """Verify favorable/neutral/tough thresholds for 32 teams."""
    records = []
    for i in range(32):
        records.append(("REG", "WR", f"T{i:02d}", 1, float(50 - i)))
    grades = compute_def_grades(_make_weekly_df(records))

    top    = grades[grades["defense_team"] == "T00"].iloc[0]   # rank 1  → favorable
    bottom = grades[grades["defense_team"] == "T31"].iloc[0]   # rank 32 → tough
    mid    = grades[grades["defense_team"] == "T15"].iloc[0]   # rank 16 → neutral

    assert top["grade"]    == "favorable"
    assert bottom["grade"] == "tough"
    assert mid["grade"]    == "neutral"


def test_compute_def_grades_filters_non_skill_positions():
    records = [
        ("REG", "QB", "TEAM", 1, 100.0),   # excluded
        ("REG", "WR", "TEAM", 1, 30.0),    # included
    ]
    grades = compute_def_grades(_make_weekly_df(records))
    assert "QB" not in grades["position"].unique()
    assert "WR" in grades["position"].unique()


def test_compute_def_grades_skips_postseason():
    records = [
        ("POST", "WR", "DEF", 20, 50.0),  # postseason — excluded
        ("REG",  "WR", "DEF",  1, 20.0),  # regular — included
    ]
    grades = compute_def_grades(_make_weekly_df(records))
    row = grades[grades["defense_team"] == "DEF"]
    assert not row.empty
    assert abs(row.iloc[0]["ppr_per_game"] - 20.0) < 0.01


def test_compute_def_grades_multiple_positions():
    """WR and RB are graded independently."""
    records = [
        ("REG", "WR", "DEF", 1, 30.0),
        ("REG", "RB", "DEF", 1, 15.0),
    ]
    grades = compute_def_grades(_make_weekly_df(records))
    assert "WR" in grades["position"].unique()
    assert "RB" in grades["position"].unique()


# ---------------------------------------------------------------------------
# is_weather_risk_game — additional coverage
# ---------------------------------------------------------------------------

def test_is_weather_risk_early_week_no_risk():
    assert is_weather_risk_game("BUF", 9, "outdoors") is False


def test_is_weather_risk_dome_no_risk():
    assert is_weather_risk_game("BUF", 12, "dome")       is False
    assert is_weather_risk_game("GB",  15, "closed")     is False
    assert is_weather_risk_game("CHI", 11, "retractable") is False


def test_is_weather_risk_no_roof_cold_city_late_week():
    """If roof data is missing (None), cold city + late week still flags risk."""
    assert is_weather_risk_game("BUF", 12, None) is True


def test_is_weather_risk_warm_city_no_risk():
    assert is_weather_risk_game("MIA", 16, "outdoors") is False
    assert is_weather_risk_game("LV",  12, "outdoors") is False


def test_weather_risk_teams_contents():
    for team in ("BUF", "GB", "CHI", "NE", "CLE", "PIT"):
        assert team in WEATHER_RISK_TEAMS, f"{team} should be in WEATHER_RISK_TEAMS"
    assert "MIA" not in WEATHER_RISK_TEAMS
    assert "LV"  not in WEATHER_RISK_TEAMS


# ---------------------------------------------------------------------------
# lookup_def_grade
# ---------------------------------------------------------------------------

def test_lookup_def_grade_empty_returns_neutral():
    assert lookup_def_grade(pd.DataFrame(), "MIN", "WR") == "neutral"


def test_lookup_def_grade_found():
    df = pd.DataFrame([
        {"defense_team": "MIN", "position": "WR", "ppr_per_game": 30.0, "rank": 5,  "grade": "favorable"},
        {"defense_team": "CHI", "position": "WR", "ppr_per_game": 12.0, "rank": 28, "grade": "tough"},
    ])
    assert lookup_def_grade(df, "MIN", "WR") == "favorable"
    assert lookup_def_grade(df, "CHI", "WR") == "tough"


def test_lookup_def_grade_not_found_returns_neutral():
    df = pd.DataFrame([
        {"defense_team": "MIN", "position": "WR", "ppr_per_game": 30.0, "rank": 5, "grade": "favorable"},
    ])
    assert lookup_def_grade(df, "XYZ", "WR") == "neutral"
    assert lookup_def_grade(df, "MIN", "RB") == "neutral"


# ---------------------------------------------------------------------------
# _get_bye_week
# ---------------------------------------------------------------------------

def test_get_bye_week_no_schedule_returns_none():
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["schedule_df"] = pd.DataFrame()
    assert agent._get_bye_week("LAC") is None


def test_get_bye_week_finds_week_7():
    agent = ScheduleAgent(dry_run=True)
    rows = [
        {"home_team": "KC" if wk % 2 else "OPP", "away_team": "OPP" if wk % 2 else "KC",
         "week": wk, "game_type": "REG"}
        for wk in range(1, 18) if wk != 7
    ]
    agent._data_cache["schedule_df"] = _make_schedule_df(rows)
    assert agent._get_bye_week("KC") == 7


# ---------------------------------------------------------------------------
# _get_team_roster
# ---------------------------------------------------------------------------

def test_get_team_roster_returns_skill_positions_only():
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["rosters_2025"] = pd.DataFrame({
        "full_name": ["Justin Jefferson", "Dalvin Cook", "John Sullivan"],
        "team":      ["MIN",              "MIN",         "MIN"],
        "position":  ["WR",               "RB",          "OL"],
    })
    roster = agent._get_team_roster("MIN", 2025)
    names = [p["name"] for p in roster]
    assert "Justin Jefferson" in names
    assert "Dalvin Cook"       in names
    assert "John Sullivan"    not in names


def test_get_team_roster_no_cache_returns_empty():
    agent = ScheduleAgent(dry_run=True)
    # No rosters key in cache
    assert agent._get_team_roster("LAC", 2025) == []


def test_get_team_roster_filters_by_team():
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["rosters_2025"] = pd.DataFrame({
        "full_name": ["Justin Jefferson", "Davante Adams"],
        "team":      ["MIN",              "LV"],
        "position":  ["WR",               "WR"],
    })
    roster = agent._get_team_roster("MIN", 2025)
    assert len(roster) == 1
    assert roster[0]["name"] == "Justin Jefferson"


# ---------------------------------------------------------------------------
# _get_team_schedule_weeks
# ---------------------------------------------------------------------------

def test_get_team_schedule_weeks_sorted():
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["schedule_df"] = _make_schedule_df([
        {"home_team": "BUF", "away_team": "MIA", "week": 3},
        {"home_team": "NE",  "away_team": "BUF", "week": 1},
    ])
    agent._data_cache["def_grades"] = pd.DataFrame(
        columns=["defense_team", "position", "ppr_per_game", "rank", "grade"]
    )
    weeks = agent._get_team_schedule_weeks("BUF")
    assert [w["week"] for w in weeks] == [1, 3]


def test_get_team_schedule_weeks_empty_cache_returns_empty():
    agent = ScheduleAgent(dry_run=True)
    assert agent._get_team_schedule_weeks("BUF") == []


def test_get_team_schedule_weeks_weather_risk_flag():
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["schedule_df"] = _make_schedule_df([
        {"home_team": "BUF", "away_team": "MIA", "week": 12, "roof": "outdoors"},
        {"home_team": "MIA", "away_team": "BUF", "week": 3,  "roof": "outdoors"},
    ])
    agent._data_cache["def_grades"] = pd.DataFrame(
        columns=["defense_team", "position", "ppr_per_game", "rank", "grade"]
    )
    weeks  = agent._get_team_schedule_weeks("BUF")
    w12    = next(w for w in weeks if w["week"] == 12)
    w3     = next(w for w in weeks if w["week"] == 3)
    assert w12["weather_risk"] is True   # BUF home, week 12 outdoor
    assert w3["weather_risk"]  is False  # week 3 < 10


def test_get_team_schedule_weeks_divisional_flag():
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["schedule_df"] = _make_schedule_df([
        {"home_team": "BUF", "away_team": "MIA", "week": 2, "div_game": 1},
        {"home_team": "BUF", "away_team": "TEN", "week": 4, "div_game": 0},
    ])
    agent._data_cache["def_grades"] = pd.DataFrame(
        columns=["defense_team", "position", "ppr_per_game", "rank", "grade"]
    )
    weeks = agent._get_team_schedule_weeks("BUF")
    w2 = next(w for w in weeks if w["week"] == 2)
    w4 = next(w for w in weeks if w["week"] == 4)
    assert w2["divisional"] is True
    assert w4["divisional"] is False


# ---------------------------------------------------------------------------
# _ensure_cache_loaded
# ---------------------------------------------------------------------------

def test_ensure_cache_loaded_skips_if_already_loaded():
    agent = ScheduleAgent(dry_run=True)
    agent._data_cache["schedule_df"]  = pd.DataFrame({"x": [1]})
    agent._data_cache["def_grades"]   = pd.DataFrame()
    agent._data_cache["rosters_2025"] = pd.DataFrame()

    with patch.object(agent, "_load_schedule")    as mock_load, \
         patch.object(agent, "_load_def_grades")  as mock_def:
        agent._ensure_cache_loaded(2025, 2026)

    mock_load.assert_not_called()
    mock_def.assert_not_called()


def test_ensure_cache_loaded_fetches_when_missing():
    agent = ScheduleAgent(dry_run=True)

    with patch.object(agent, "_load_schedule")   as mock_load, \
         patch.object(agent, "_load_def_grades") as mock_def, \
         patch("backend.agents.schedule.nfl_data.fetch_rosters", return_value=pd.DataFrame()):
        agent._ensure_cache_loaded(2025, 2026)

    mock_load.assert_called_once_with(2026, 2025)
    mock_def.assert_called_once_with(2025)


# ---------------------------------------------------------------------------
# _load_schedule
# ---------------------------------------------------------------------------

def test_load_schedule_uses_analysis_year_when_available():
    agent    = ScheduleAgent(dry_run=True)
    mock_df  = _make_schedule_df([{"home_team": "KC", "away_team": "BUF", "week": 1}])

    with patch("backend.agents.schedule.nfl_data.fetch_schedules", return_value=mock_df) as mock_fetch:
        agent._load_schedule(2026, 2025)

    mock_fetch.assert_called_once_with(2026)
    assert agent._data_cache.get("schedule_year") == 2026


def test_load_schedule_falls_back_to_current_season():
    agent      = ScheduleAgent(dry_run=True)
    empty_df   = pd.DataFrame()
    current_df = _make_schedule_df([{"home_team": "KC", "away_team": "BUF", "week": 1}])

    with patch("backend.agents.schedule.nfl_data.fetch_schedules",
               side_effect=[empty_df, current_df]) as mock_fetch:
        agent._load_schedule(2026, 2025)

    assert mock_fetch.call_count == 2
    assert agent._data_cache.get("schedule_year") == 2025


def test_load_schedule_logs_warning_on_fallback():
    agent      = ScheduleAgent(dry_run=True)
    empty_df   = pd.DataFrame()
    fallback_df = _make_schedule_df([{"home_team": "MIN", "away_team": "GB", "week": 1}])

    with patch("backend.agents.schedule.nfl_data.fetch_schedules",
               side_effect=[empty_df, fallback_df]), \
         patch("backend.agents.schedule.logger") as mock_log:
        agent._load_schedule(2026, 2025)

    mock_log.warning.assert_called()


def test_load_schedule_sets_empty_when_both_fail():
    agent = ScheduleAgent(dry_run=True)

    with patch("backend.agents.schedule.nfl_data.fetch_schedules",
               side_effect=Exception("network error")):
        agent._load_schedule(2026, 2025)

    assert "schedule_df" in agent._data_cache
    assert agent._data_cache["schedule_df"].empty


# ---------------------------------------------------------------------------
# _load_def_grades
# ---------------------------------------------------------------------------

def test_load_def_grades_stores_grades():
    agent      = ScheduleAgent(dry_run=True)
    weekly_df  = _make_weekly_df([("REG", "WR", "MIN", 1, 30.0)])

    with patch("backend.agents.schedule.nfl_data.fetch_weekly_stats", return_value=weekly_df):
        agent._load_def_grades(2025)

    assert "def_grades" in agent._data_cache
    assert not agent._data_cache["def_grades"].empty


def test_load_def_grades_stores_empty_on_failure():
    agent = ScheduleAgent(dry_run=True)

    with patch("backend.agents.schedule.nfl_data.fetch_weekly_stats",
               side_effect=Exception("no data")):
        agent._load_def_grades(2025)

    assert "def_grades" in agent._data_cache
    assert agent._data_cache["def_grades"].empty


# ---------------------------------------------------------------------------
# _to_decimal
# ---------------------------------------------------------------------------

def test_to_decimal_valid_float():
    assert _to_decimal(8.5)  == Decimal("8.5")
    assert _to_decimal(10.0) == Decimal("10.0")


def test_to_decimal_valid_string():
    assert _to_decimal("7.3") == Decimal("7.3")


def test_to_decimal_none_returns_none():
    assert _to_decimal(None) is None


def test_to_decimal_invalid_returns_none():
    assert _to_decimal("not-a-number") is None
    assert _to_decimal([])             is None


# ---------------------------------------------------------------------------
# _bulk_resolve_player_ids
# ---------------------------------------------------------------------------

async def test_bulk_resolve_player_ids_empty_list():
    mock_session = AsyncMock()
    result = await _bulk_resolve_player_ids(mock_session, [])
    assert result == {}
    mock_session.execute.assert_not_called()


async def test_bulk_resolve_player_ids_single_player():
    mock_player = _make_player("Justin Jefferson", "MIN")
    r = MagicMock(); r.scalars.return_value.all.return_value = [mock_player]
    mock_session = AsyncMock(); mock_session.execute = AsyncMock(return_value=r)

    id_map = await _bulk_resolve_player_ids(mock_session, [("Justin Jefferson", "MIN")])
    assert id_map[("Justin Jefferson", "MIN")] == str(mock_player.id)


async def test_bulk_resolve_player_ids_team_disambiguation():
    """Two players with same last name → team_abbr picks correct one."""
    p_lac = _make_player("Mike Williams", "LAC"); p_lac.id = "id-lac"; p_lac.team_abbr = "LAC"
    p_ten = _make_player("Mike Williams", "TEN"); p_ten.id = "id-ten"; p_ten.team_abbr = "TEN"

    r = MagicMock(); r.scalars.return_value.all.return_value = [p_lac, p_ten]
    mock_session = AsyncMock(); mock_session.execute = AsyncMock(return_value=r)

    id_map = await _bulk_resolve_player_ids(mock_session, [("Mike Williams", "TEN")])
    assert id_map[("Mike Williams", "TEN")] == "id-ten"


async def test_bulk_resolve_player_ids_no_match_returns_none():
    r = MagicMock(); r.scalars.return_value.all.return_value = []
    mock_session = AsyncMock(); mock_session.execute = AsyncMock(return_value=r)

    id_map = await _bulk_resolve_player_ids(mock_session, [("Ghost Player", "XYZ")])
    assert id_map[("Ghost Player", "XYZ")] is None


# ---------------------------------------------------------------------------
# _write_schedules
# ---------------------------------------------------------------------------

async def test_write_schedules_empty_result_returns_zero():
    assert await _write_schedules({}, {"players": []}, "LAC") == 0


async def test_write_schedules_skips_unresolved_player():
    result  = _make_result()
    context = {"players": [{"name": "Ghost Player", "position": "WR"}], "bye_week": 9}

    r_bulk = MagicMock(); r_bulk.scalars.return_value.all.return_value = []
    mock_session = AsyncMock(); mock_session.execute = AsyncMock(return_value=r_bulk)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.schedule.AsyncSessionLocal", return_value=mock_ctx), \
         patch("backend.agents.schedule.get_analysis_year", return_value=2026):
        written = await _write_schedules(result, context, "LAC")

    assert written == 0


async def test_write_schedules_inserts_new_record():
    result  = _make_result(wr_grade="favorable")
    context = {"players": [{"name": "Justin Jefferson", "position": "WR"}], "bye_week": 9}

    mock_player = _make_player("Justin Jefferson", "MIN")
    r_bulk      = MagicMock(); r_bulk.scalars.return_value.all.return_value = [mock_player]
    r_no_exist  = MagicMock(); r_no_exist.scalars.return_value.first.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_exist])
    mock_session.add    = MagicMock()   # sync — not awaited in production code
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.schedule.AsyncSessionLocal", return_value=mock_ctx), \
         patch("backend.agents.schedule.get_analysis_year", return_value=2026):
        written = await _write_schedules(result, context, "MIN")

    assert written == 1
    mock_session.add.assert_called_once()
    mock_session.commit.assert_called_once()


async def test_write_schedules_updates_existing_record():
    result   = _make_result(te_grade="neutral")
    context  = {"players": [{"name": "Travis Kelce", "position": "TE"}], "bye_week": 6}

    mock_player = _make_player("Travis Kelce", "KC", "TE")
    existing    = MagicMock()
    existing.playoff_window_grade = "favorable"  # old value

    r_bulk   = MagicMock(); r_bulk.scalars.return_value.all.return_value = [mock_player]
    r_exist  = MagicMock(); r_exist.scalars.return_value.first.return_value = existing

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_exist])
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.schedule.AsyncSessionLocal", return_value=mock_ctx), \
         patch("backend.agents.schedule.get_analysis_year", return_value=2026):
        written = await _write_schedules(result, context, "KC")

    assert written == 1
    mock_session.add.assert_not_called()           # in-place update, not a new record
    assert existing.playoff_window_grade == "neutral"   # TE block applied


async def test_write_schedules_qb_uses_wr_grades():
    """QBs should receive WR position-group grades (pass defense context)."""
    result  = _make_result(wr_grade="favorable", rb_grade="tough")
    context = {"players": [{"name": "Justin Herbert", "position": "QB"}], "bye_week": 9}

    mock_player = _make_player("Justin Herbert", "LAC", "QB")
    r_bulk      = MagicMock(); r_bulk.scalars.return_value.all.return_value = [mock_player]
    r_no_exist  = MagicMock(); r_no_exist.scalars.return_value.first.return_value = None

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_exist])

    added: list = []
    mock_session.add = MagicMock(side_effect=added.append)  # sync mock

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.schedule.AsyncSessionLocal", return_value=mock_ctx), \
         patch("backend.agents.schedule.get_analysis_year", return_value=2026):
        written = await _write_schedules(result, context, "LAC")

    assert written == 1
    qb_rec = added[0]
    # QB uses WR grades ("favorable"), not RB grades ("tough")
    assert qb_rec.early_window_grade == "favorable"


# ---------------------------------------------------------------------------
# run_for_team — edge cases
# ---------------------------------------------------------------------------

async def test_run_for_team_skips_when_no_players():
    agent = ScheduleAgent(dry_run=False)

    with patch.object(agent, "_build_team_context", new_callable=AsyncMock) as mock_ctx, \
         patch.object(agent, "call_once", new_callable=AsyncMock) as mock_call:
        mock_ctx.return_value = {
            "players":       [],
            "schedule":      {"full_season": [{"week": 1}]},
            "analysis_year": 2026,
            "schedule_year": 2025,
        }
        result = await agent.run_for_team("LAC")

    assert result == 0
    mock_call.assert_not_called()


async def test_run_for_team_skips_when_no_schedule():
    agent = ScheduleAgent(dry_run=False)

    with patch.object(agent, "_build_team_context", new_callable=AsyncMock) as mock_ctx, \
         patch.object(agent, "call_once", new_callable=AsyncMock) as mock_call:
        mock_ctx.return_value = {
            "players":       [{"name": "X", "position": "WR"}],
            "schedule":      {"full_season": []},
            "analysis_year": 2026,
            "schedule_year": 2025,
        }
        result = await agent.run_for_team("LAC")

    assert result == 0
    mock_call.assert_not_called()


async def test_run_for_team_dry_run_returns_zero():
    agent = ScheduleAgent(dry_run=True)

    with patch.object(agent, "_build_team_context", new_callable=AsyncMock) as mock_ctx, \
         patch.object(agent, "call_once", new_callable=AsyncMock, return_value=None):
        mock_ctx.return_value = {
            "players":       [{"name": "X", "position": "WR"}],
            "schedule":      {"full_season": [{"week": 1}]},
            "analysis_year": 2026,
            "schedule_year": 2025,
        }
        result = await agent.run_for_team("LAC")

    assert result == 0


# ---------------------------------------------------------------------------
# run_all_teams
# ---------------------------------------------------------------------------

async def test_run_all_teams_runs_all_32_teams():
    from backend.agents.team_systems import NFL_TEAMS

    agent = ScheduleAgent(dry_run=False)

    with patch.object(agent, "_load_schedule"), \
         patch.object(agent, "_load_def_grades"), \
         patch("backend.agents.schedule.nfl_data.fetch_rosters", return_value=pd.DataFrame()), \
         patch.object(agent, "run_for_team", new_callable=AsyncMock, return_value=3) as mock_run, \
         patch("backend.agents.schedule.get_current_season", return_value=2025), \
         patch("backend.agents.schedule.get_analysis_year",  return_value=2026):
        results = await agent.run_all_teams(concurrency=8)

    assert mock_run.call_count == 32
    called_teams = {call.args[0] for call in mock_run.call_args_list}
    assert called_teams == set(NFL_TEAMS)


# ---------------------------------------------------------------------------
# Module shims
# ---------------------------------------------------------------------------

def test_module_shims_are_async():
    import inspect
    from backend.agents import schedule
    assert inspect.iscoroutinefunction(schedule.run_for_team)
    assert inspect.iscoroutinefunction(schedule.run_all_teams)


def test_get_agent_returns_schedule_agent():
    agent = _get_agent(dry_run=True)
    assert isinstance(agent, ScheduleAgent)
    assert agent.dry_run is True


# ---------------------------------------------------------------------------
# bye_in_playoff_window — Gap 2 fix
# ---------------------------------------------------------------------------

async def test_bye_in_playoff_window_true_when_bye_in_weeks_14_to_17():
    """bye_in_playoff_window must be True when bye falls in the playoff window (weeks 14-17)."""
    from backend.models.player import PlayerSchedule

    assert hasattr(PlayerSchedule, "bye_in_playoff_window"), (
        "PlayerSchedule.bye_in_playoff_window is missing — add it as a first-class column"
    )

    result  = _make_result(bye_week=14)
    context = {"players": [{"name": "Justin Jefferson", "position": "WR"}], "bye_week": 14}

    mock_player = _make_player("Justin Jefferson", "MIN")
    r_bulk      = MagicMock(); r_bulk.scalars.return_value.all.return_value = [mock_player]
    r_no_exist  = MagicMock(); r_no_exist.scalars.return_value.first.return_value = None

    added: list = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_exist])
    mock_session.add     = MagicMock(side_effect=added.append)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.schedule.AsyncSessionLocal", return_value=mock_ctx), \
         patch("backend.agents.schedule.get_analysis_year", return_value=2026):
        await _write_schedules(result, context, "MIN")

    assert len(added) == 1
    assert added[0].bye_in_playoff_window is True


async def test_bye_in_playoff_window_false_when_bye_not_in_playoff_window():
    """bye_in_playoff_window must be False when bye is NOT in weeks 14-17."""
    result  = _make_result(bye_week=9)
    context = {"players": [{"name": "Davante Adams", "position": "WR"}], "bye_week": 9}

    mock_player = _make_player("Davante Adams", "CHI")
    r_bulk      = MagicMock(); r_bulk.scalars.return_value.all.return_value = [mock_player]
    r_no_exist  = MagicMock(); r_no_exist.scalars.return_value.first.return_value = None

    added: list = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[r_bulk, r_no_exist])
    mock_session.add     = MagicMock(side_effect=added.append)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_ctx.__aexit__  = AsyncMock(return_value=False)

    with patch("backend.agents.schedule.AsyncSessionLocal", return_value=mock_ctx), \
         patch("backend.agents.schedule.get_analysis_year", return_value=2026):
        await _write_schedules(result, context, "CHI")

    assert len(added) == 1
    assert added[0].bye_in_playoff_window is False
