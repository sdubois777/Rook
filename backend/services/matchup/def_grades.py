"""
As-of-week defensive grades — the point-in-time matchup signal for the Tier-1
start/sit panel. Pure/deterministic, ZERO-metered.

The existing ``compute_def_grades`` (agents/schedule.py) is SEASON-STATIC: it means
per-position PPR-allowed over ALL weeks in the frame, so applied to a historical week
it leaks future weeks (look-ahead). And the demo's 2025 season can't use
``import_weekly_data`` (nflverse 404s that year).

Both are solved the same way, mirroring the DST tilt's ``build_offense_signal``
``upto_week`` discipline (kdef_matchup.py): build a ``compute_def_grades``-shaped
weekly frame FROM PBP over weeks ``1..week-1`` ONLY, then feed the UNCHANGED
``compute_def_grades`` (same output: defense_team, position, ppr_per_game, rank,
grade). Existing callers of compute_def_grades are untouched.

Coverage = WR/RB/TE (what compute_def_grades grades). QB/K/DEF get no grade here.
"""
from __future__ import annotations

import logging

import pandas as pd

from backend.agents.schedule import compute_def_grades

logger = logging.getLogger(__name__)

_GRADE_POSITIONS = ("WR", "RB", "TE")


def _opponent_by_team_week(schedule: pd.DataFrame) -> dict[tuple[str, int], str]:
    """{(team_abbr, week) -> opponent_abbr} across the whole schedule (REG only)."""
    out: dict[tuple[str, int], str] = {}
    if schedule is None or schedule.empty:
        return out
    reg = schedule
    if "game_type" in schedule.columns:
        reg = schedule[schedule["game_type"] == "REG"]
    for r in reg.itertuples():
        wk = int(getattr(r, "week"))
        home, away = getattr(r, "home_team", None), getattr(r, "away_team", None)
        if home and away:
            out[(str(home), wk)] = str(away)
            out[(str(away), wk)] = str(home)
    return out


def build_weekly_ppr_by_defense(season: int, upto_week: int) -> pd.DataFrame:
    """A ``compute_def_grades``-shaped weekly frame from PBP over weeks 1..upto_week-1
    ONLY (holdout-safe). Columns: opponent_team (the DEFENSE faced), position, week,
    fantasy_points_ppr, season_type. Loud-warns players/weeks that can't be mapped;
    never silently keeps an unmapped row (they're dropped after warning)."""
    from backend.integrations.nfl_data import fetch_schedules, fetch_seasonal_rosters
    from backend.integrations.nfl_weekly import compute_weekly_pbp

    empty = pd.DataFrame(columns=["opponent_team", "position", "week",
                                  "fantasy_points_ppr", "season_type"])
    weekly = compute_weekly_pbp(season)   # per (player_id gsis, recent_team, week) REG PPR
    if weekly is None or weekly.empty:
        logger.warning("as_of_week def grades: no weekly PBP for %d — no grades", season)
        return empty

    # HOLDOUT: strictly BEFORE the current week. This is the look-ahead guard.
    weekly = weekly[weekly["week"] < int(upto_week)].copy()
    if weekly.empty:
        logger.warning("as_of_week def grades: no weeks < %d for %d — no grades", upto_week, season)
        return empty

    # position: gsis -> position from the season roster (WR/RB/TE are what we grade).
    rosters = fetch_seasonal_rosters(season)
    if rosters is None or rosters.empty or "position" not in rosters.columns:
        logger.warning("as_of_week def grades: no seasonal rosters for %d — cannot position players", season)
        return empty
    posmap = {str(pid): pos for pid, pos in zip(rosters["player_id"].astype(str), rosters["position"])}
    weekly["position"] = weekly["player_id"].astype(str).map(posmap)

    # opponent (the defense faced): (recent_team, week) -> opponent.
    opp = _opponent_by_team_week(fetch_schedules(season))
    weekly["opponent_team"] = [
        opp.get((str(t), int(w))) for t, w in zip(weekly["recent_team"], weekly["week"])
    ]

    # Loud-warn drops — never silent.
    no_pos = int(weekly["position"].isna().sum())
    no_opp = int(weekly["opponent_team"].isna().sum())
    if no_pos:
        logger.warning("as_of_week def grades %d wk<%d: %d player-weeks with no roster position (dropped)",
                       season, upto_week, no_pos)
    if no_opp:
        logger.warning("as_of_week def grades %d wk<%d: %d player-weeks with no scheduled opponent (dropped)",
                       season, upto_week, no_opp)

    weekly = weekly[weekly["position"].isin(_GRADE_POSITIONS) & weekly["opponent_team"].notna()].copy()
    weekly["season_type"] = "REG"
    return weekly[["opponent_team", "position", "week", "fantasy_points_ppr", "season_type"]]


def as_of_week_def_grades(season: int, week: int) -> pd.DataFrame:
    """Point-in-time defensive grades from weeks 1..week-1 ONLY (no look-ahead), in
    the SAME shape as compute_def_grades (defense_team, position, ppr_per_game, rank,
    grade) so ``lookup_def_grade`` works unchanged. Empty frame if data is unavailable
    (callers must default to neutral / no-tag)."""
    weekly = build_weekly_ppr_by_defense(season, week)
    return compute_def_grades(weekly)
