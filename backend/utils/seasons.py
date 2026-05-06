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

import logging
from datetime import date

logger = logging.getLogger(__name__)


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


def get_fantasypros_auction_year() -> tuple[int, bool]:
    """
    Determine which year's FantasyPros auction data to pull,
    with automatic fallback.

    FantasyPros publishes current season auction values starting
    around late June/early July when analysts have processed the
    full offseason. Before that, current year data either doesn't
    exist or is too sparse to be useful.

    Returns:
        (year, is_current_season)
        is_current_season=False means we're using fallback data.

    Examples (called in May 2026):  → (2025, False)
    Examples (called in August 2026): → (2026, True)

    Note: get_current_season() already returns the most recently
    completed season (e.g. 2025 in May 2026), so we return it
    directly. The is_current_season flag indicates whether FP
    has published fresh data for the upcoming draft season yet
    (typically starting in July).
    """
    today = date.today()
    current_season = get_current_season()

    if today.month >= 7:
        return current_season, True
    else:
        return current_season, False


async def get_best_available_auction_year(
    scraper_fn,
    format: str = "ppr",
) -> tuple[list, int, bool]:
    """
    Try preferred year first, fall back to previous year if the
    preferred year returns no data or too few players.

    Args:
        scraper_fn: async function(format, year) → list of player values
        format: scoring format string

    Returns:
        (values, year_used, is_current_season)

    Minimum viable result: 100+ players.
    """
    preferred_year, is_current = get_fantasypros_auction_year()

    try:
        values = await scraper_fn(format, preferred_year)
        if len(values) >= 100:
            logger.info(
                "FantasyPros: using %d data (%d players)",
                preferred_year, len(values),
            )
            return values, preferred_year, is_current
        else:
            logger.warning(
                "FantasyPros: %d returned only %d players — falling back to %d",
                preferred_year, len(values), preferred_year - 1,
            )
    except Exception as e:
        logger.warning(
            "FantasyPros: %d failed (%s) — falling back to %d",
            preferred_year, e, preferred_year - 1,
        )

    fallback_year = preferred_year - 1
    values = await scraper_fn(format, fallback_year)
    logger.info(
        "FantasyPros: using fallback %d data (%d players)",
        fallback_year, len(values),
    )
    return values, fallback_year, False


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
