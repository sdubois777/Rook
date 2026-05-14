"""
tests/unit/test_seasons.py

All required named test cases from stage-01-foundation.md.
These tests use date mocking — never depend on the actual current date.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from freezegun import freeze_time

from backend.utils.seasons import (
    get_analysis_seasons,
    get_analysis_year,
    get_current_season,
    get_draft_prep_window,
    get_player_seasons_for_baseline,
    get_previous_season,
)


# ---------------------------------------------------------------------------
# get_current_season()
# ---------------------------------------------------------------------------

def test_current_season_january():
    with freeze_time("2026-01-15"):
        assert get_current_season() == 2025


def test_current_season_february():
    with freeze_time("2026-02-09"):
        assert get_current_season() == 2025


def test_current_season_march():
    with freeze_time("2026-03-01"):
        assert get_current_season() == 2026


def test_current_season_august():
    with freeze_time("2026-08-15"):
        assert get_current_season() == 2026


def test_current_season_december():
    with freeze_time("2026-12-15"):
        assert get_current_season() == 2026


# ---------------------------------------------------------------------------
# get_analysis_year()
# ---------------------------------------------------------------------------

def test_analysis_year_equals_current_season():
    """analysis_year == current_season (we draft FOR the current season)."""
    assert get_analysis_year() == get_current_season()


def test_analysis_year_may_2026():
    """In May 2026, analysis_year should be 2026 (not 2027)."""
    with freeze_time("2026-05-11"):
        assert get_analysis_year() == 2026


def test_analysis_year_january_2026():
    """In January 2026, still in 2025 season, analysis_year = 2025."""
    with freeze_time("2026-01-15"):
        assert get_analysis_year() == 2025


def test_analysis_year_august_2026():
    """In August 2026, analysis_year = 2026 (draft month)."""
    with freeze_time("2026-08-15"):
        assert get_analysis_year() == 2026


# ---------------------------------------------------------------------------
# get_analysis_seasons()
# ---------------------------------------------------------------------------

def test_analysis_seasons_march_2026():
    with freeze_time("2026-03-01"):
        assert get_analysis_seasons(3) == [2023, 2024, 2025]


def test_analysis_seasons_august_2026():
    with freeze_time("2026-08-15"):
        assert get_analysis_seasons(3) == [2023, 2024, 2025]


def test_analysis_seasons_january_2026():
    with freeze_time("2026-01-15"):
        assert get_analysis_seasons(3) == [2022, 2023, 2024]


def test_analysis_seasons_february_2026():
    with freeze_time("2026-02-09"):
        assert get_analysis_seasons(3) == [2022, 2023, 2024]


def test_analysis_seasons_never_includes_current():
    seasons = get_analysis_seasons(3)
    assert get_current_season() not in seasons


def test_analysis_seasons_returns_correct_lookback():
    """get_analysis_seasons(3) returns exactly 3 seasons."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 4, 30)
        seasons = get_analysis_seasons(3)
        assert len(seasons) == 3


def test_analysis_seasons_five_season_lookback():
    """get_analysis_seasons(5) returns 5 completed seasons before current."""
    with freeze_time("2026-04-30"):
        seasons = get_analysis_seasons(5)
        assert len(seasons) == 5
        assert seasons == sorted(seasons)  # must be ascending
        assert seasons == [2021, 2022, 2023, 2024, 2025]


def test_analysis_seasons_correct_count():
    """Lookback count is exact."""
    with freeze_time("2026-05-15"):
        assert len(get_analysis_seasons(3)) == 3
        assert len(get_analysis_seasons(5)) == 5


# ---------------------------------------------------------------------------
# get_draft_prep_window()
# ---------------------------------------------------------------------------

def test_get_draft_prep_window_returns_all_fields():
    """get_draft_prep_window() must return all four expected keys."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 4, 30)
        window = get_draft_prep_window()
        assert "current_season" in window
        assert "previous_season" in window
        assert "analysis_year" in window
        assert "analysis_seasons" in window
        assert isinstance(window["analysis_seasons"], list)


# ---------------------------------------------------------------------------
# Codebase scanner — no hardcoded years in agent files
# ---------------------------------------------------------------------------

def test_no_hardcoded_years_in_agent_files():
    """
    Scan all agent Python files for hardcoded year integers (2022-2029).
    Any found outside of seasons.py itself is a bug.

    Excludes:
    - seasons.py (the one allowed source of truth)
    - model strings (claude-haiku-4-5-20251001 contains 2025 — acceptable)
    - Comments referencing years for documentation purposes
    """
    agents_dir = Path(__file__).parent.parent.parent / "backend" / "agents"
    year_pattern = re.compile(r"\b(202[2-9])\b")
    # Model strings are OK — they contain year-like digits as part of the name
    model_pattern = re.compile(r"claude-[a-z]+-[\d]+-[\d]+-\w+")

    violations: list[str] = []

    for py_file in agents_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            # Skip comment lines
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Remove model strings before checking
            cleaned = model_pattern.sub("", line)
            if year_pattern.search(cleaned):
                violations.append(f"{py_file.name}:{lineno}: {line.strip()}")

    assert not violations, (
        "Hardcoded year integers found in agent files:\n"
        + "\n".join(violations)
        + "\nFix: use get_current_season(), get_analysis_seasons(), or get_analysis_year()"
    )


# ---------------------------------------------------------------------------
# Seed script — no hardcoded years
# ---------------------------------------------------------------------------

def test_seed_nfl_data_uses_dynamic_seasons():
    """Verify no hardcoded [2022, 2023, 2024] exists in scripts/seed_nfl_data.py."""
    source = (Path(__file__).parent.parent.parent / "scripts" / "seed_nfl_data.py").read_text()
    assert "[2022, 2023, 2024]" not in source, "seed_nfl_data.py still has hardcoded [2022, 2023, 2024]"
    assert "[2023, 2024]" not in source, "seed_nfl_data.py still has hardcoded [2023, 2024]"


def test_get_analysis_seasons_returns_3_consecutive_seasons():
    """get_analysis_seasons(3) returns a list of 3 consecutive seasons."""
    with freeze_time("2026-08-01"):
        seasons = get_analysis_seasons(3)
        assert len(seasons) == 3
        assert seasons[1] - seasons[0] == 1
        assert seasons[2] - seasons[1] == 1


# ---------------------------------------------------------------------------
# get_player_seasons_for_baseline()
# ---------------------------------------------------------------------------

def test_veteran_gets_extended_lookback():
    """9-season player gets up to 6 seasons to load."""
    with freeze_time("2026-05-14"):
        seasons = get_player_seasons_for_baseline(9)
        assert len(seasons) == 6  # 4 target + 2 buffer
        assert max(seasons) == 2025  # most recent completed
        assert min(seasons) == 2020  # 6 back from 2025


def test_young_player_capped_by_career():
    """2-season player only gets 2 seasons (career caps max_load)."""
    with freeze_time("2026-05-14"):
        seasons = get_player_seasons_for_baseline(2)
        assert len(seasons) == 2  # min(2, min(2,4)+2) = min(2,4) = 2
        assert seasons == [2024, 2025]


def test_rookie_gets_one_season():
    with freeze_time("2026-05-14"):
        seasons = get_player_seasons_for_baseline(1)
        assert len(seasons) == 1
        assert seasons[0] == 2025


def test_none_seasons_played_treated_as_rookie():
    with freeze_time("2026-05-14"):
        seasons = get_player_seasons_for_baseline(None)
        assert len(seasons) == 1
        assert seasons[0] == 2025


def test_four_season_player_gets_full_career():
    """4-season player: target=4, buffer=2, career=4.
    max_load = min(4, 4+2) = 4 (capped by career)."""
    with freeze_time("2026-05-14"):
        seasons = get_player_seasons_for_baseline(4)
        assert len(seasons) == 4
        assert seasons == [2022, 2023, 2024, 2025]


def test_player_seasons_in_ascending_order():
    with freeze_time("2026-05-14"):
        seasons = get_player_seasons_for_baseline(9)
        assert seasons == sorted(seasons)
