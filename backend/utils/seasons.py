"""
Season year utilities — dynamic calculation, never hardcoded.

All agents must import from here. Never hardcode season years.

NFL season calendar:
  - Regular season: September–January
  - Offseason/draft prep: February–August
  - New season starts: September

Logic:
  - If current month >= 6 (June), the current calendar year IS the current NFL season
    (e.g. in July 2026, we're preparing for the 2026 season)
  - If current month < 6 (Jan–May), we're in the tail of the previous season
    (e.g. in March 2026, the 2025 season just ended)

Usage:
    from backend.utils.seasons import (
        get_current_season,
        get_analysis_seasons,
        get_analysis_year,
    )

    CURRENT_SEASON   = get_current_season()       # The most recently completed season
    ANALYSIS_SEASONS = get_analysis_seasons(3)    # Last N seasons for historical data
    ANALYSIS_YEAR    = get_analysis_year()        # The upcoming draft we're preparing for
"""
from __future__ import annotations

from datetime import date


def get_current_season() -> int:
    """
    Returns the most recently completed (or current) NFL season year.

    Examples (assuming standard NFL calendar):
      - Called in July 2026  → 2026  (2026 season is imminent/current)
      - Called in March 2026 → 2025  (2025 season just ended)
      - Called in August 2026 → 2026 (draft prep is for 2026 season)
    """
    today = date.today()
    return today.year if today.month >= 6 else today.year - 1


def get_analysis_year() -> int:
    """
    Returns the upcoming season year we're building the draft bible for.
    This is always one year ahead of the current season.

    Examples:
      - Called in July 2026  → 2027  (preparing for 2027 draft)
      - Called in March 2026 → 2026  (preparing for 2026 draft)
    """
    return get_current_season() + 1


def get_analysis_seasons(lookback: int = 3) -> list[int]:
    """
    Returns the last N completed seasons for historical data analysis.
    Never includes the current/upcoming season (incomplete data).

    Args:
        lookback: Number of seasons to include. Default 3.

    Examples (called in July 2026, current season = 2026):
        get_analysis_seasons(3) → [2023, 2024, 2025]
        get_analysis_seasons(5) → [2021, 2022, 2023, 2024, 2025]

    Examples (called in March 2026, current season = 2025):
        get_analysis_seasons(3) → [2022, 2023, 2024]
    """
    current = get_current_season()
    return list(range(current - lookback, current))


def get_previous_season() -> int:
    """Returns the season immediately before the current one."""
    return get_current_season() - 1


def get_draft_prep_window() -> dict[str, int]:
    """
    Returns a dict with all season year constants needed by pipeline agents.
    Convenience function to get everything at once.

    Usage:
        window = get_draft_prep_window()
        current    = window["current_season"]
        upcoming   = window["analysis_year"]
        historical = window["analysis_seasons"]  # list[int]
    """
    return {
        "current_season": get_current_season(),
        "previous_season": get_previous_season(),
        "analysis_year": get_analysis_year(),
        "analysis_seasons": get_analysis_seasons(3),
    }
