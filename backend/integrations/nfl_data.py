"""
NFL data integration — wraps nfl_data_py with a parquet cache layer.

Sync functions (fetch_*) are for scripts.
Async functions (get_*) are for the agent pipeline / FastAPI.
Cache lives in data/cache/ (gitignored).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

import pandas as pd
import nfl_data_py as nfl

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}


# ---------------------------------------------------------------------------
# Name normalization utilities — shared by all agents
# ---------------------------------------------------------------------------

def normalize_player_name(name: str) -> str:
    """
    Normalize player names for matching across data sources.
    Handles the most common NFL data name format differences:
      - Suffixes: Jr., Sr., II, III, IV
      - Double initials: D.K. → dk, A.J. → aj, J.K. → jk
      - Apostrophes: Ja'Marr → jamarr
      - Trailing/extra periods
    """
    if not name:
        return ""
    normalized = name.lower().strip()
    # Remove name suffixes at end of string
    normalized = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv)$", "", normalized)
    # Normalize double-initial patterns: "d.k." → "dk", "a.j." → "aj"
    normalized = re.sub(r"([a-z])\.([a-z])\.", r"\1\2", normalized)
    # Remove remaining periods and apostrophes
    normalized = normalized.replace(".", "").replace("'", "")
    # Collapse multiple spaces
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def build_player_lookup(players: list[dict]) -> dict[str, str]:
    """
    Build {normalized_name: player_id} from a list of player dicts.
    Each dict must have 'name' and 'id' keys (id is the DB UUID string).
    Build once per team run; reuse for all name-based matching.
    """
    lookup: dict[str, str] = {}
    for p in players:
        raw_name = p.get("name", "")
        player_id = p.get("id", "")
        if raw_name and player_id:
            lookup[normalize_player_name(raw_name)] = str(player_id)
    return lookup


def _ensure_cache():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(name: str) -> Path:
    _ensure_cache()
    return CACHE_DIR / f"{name}.parquet"


def _load_or_fetch(cache_name: str, fetch_fn) -> pd.DataFrame:
    path = _cache_path(cache_name)
    if path.exists():
        logger.debug("Cache hit: %s", cache_name)
        return pd.read_parquet(path)
    logger.info("Downloading: %s", cache_name)
    df = fetch_fn()
    df.to_parquet(path, index=False)
    return df


# ---------------------------------------------------------------------------
# Sync fetch functions
# ---------------------------------------------------------------------------

def fetch_weekly_stats(season: int) -> pd.DataFrame:
    return _load_or_fetch(
        f"weekly_{season}",
        lambda: nfl.import_weekly_data([season]),
    )


def fetch_seasonal_data(season: int) -> pd.DataFrame:
    return _load_or_fetch(
        f"seasonal_{season}",
        lambda: nfl.import_seasonal_data([season]),
    )


def fetch_snap_counts(season: int) -> pd.DataFrame:
    return _load_or_fetch(
        f"snaps_{season}",
        lambda: nfl.import_snap_counts([season]),
    )


def fetch_schedules(season: int) -> pd.DataFrame:
    return _load_or_fetch(
        f"schedules_{season}",
        lambda: nfl.import_schedules([season]),
    )


def fetch_players() -> pd.DataFrame:
    return _load_or_fetch("players", nfl.import_players)


def fetch_rosters(season: int) -> pd.DataFrame:
    return _load_or_fetch(
        f"rosters_{season}",
        lambda: nfl.import_weekly_rosters([season]),
    )


def fetch_injuries(season: int) -> pd.DataFrame:
    return _load_or_fetch(
        f"injuries_{season}",
        lambda: nfl.import_injuries([season]),
    )


def fetch_ngs_data(stat_type: str, season: int) -> pd.DataFrame:
    """stat_type: 'passing' | 'receiving' | 'rushing'"""
    return _load_or_fetch(
        f"ngs_{stat_type}_{season}",
        lambda: nfl.import_ngs_data(stat_type, [season]),
    )


def compute_target_share(season: int) -> pd.DataFrame:
    """
    Derive per-player target share and air yards share from weekly data.
    Returns one row per player with season-level averages.
    """
    cache_name = f"target_share_{season}"
    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)

    weekly = fetch_weekly_stats(season)

    # Skill positions only
    weekly = weekly[weekly["position"].isin(SKILL_POSITIONS)].copy()

    # Regular season only — nfl_data_py includes postseason weeks which inflates
    # season totals (e.g. Barkley 2024 PHI = 20 games, not 17).
    if "season_type" in weekly.columns:
        weekly = weekly[weekly["season_type"] == "REG"].copy()

    # Team-level targets per week (denominator for target share)
    team_targets = (
        weekly.groupby(["season", "week", "recent_team"])["targets"]
        .sum()
        .reset_index()
        .rename(columns={"targets": "team_targets"})
    )
    weekly = weekly.merge(team_targets, on=["season", "week", "recent_team"], how="left")

    # nfl_data_py already provides target_share and air_yards_share columns
    # Use them directly; fall back to manual calculation if absent
    if "target_share" not in weekly.columns:
        weekly["target_share"] = weekly["targets"] / weekly["team_targets"].replace(0, pd.NA)

    # Season-level aggregation
    agg = (
        weekly.groupby(["player_id", "player_name", "recent_team", "position"])
        .agg(
            games=("week", "count"),
            total_targets=("targets", "sum"),
            total_receptions=("receptions", "sum"),
            total_rec_yards=("receiving_yards", "sum"),
            total_rec_tds=("receiving_tds", "sum"),
            avg_target_share=("target_share", "mean"),
            total_air_yards=("receiving_air_yards", "sum"),
            avg_air_yards_share=("air_yards_share", "mean"),
            total_carries=("carries", "sum"),
            total_rush_yards=("rushing_yards", "sum"),
            total_rush_tds=("rushing_tds", "sum"),
            total_fantasy_points=("fantasy_points_ppr", "sum"),
        )
        .reset_index()
    )
    agg["season"] = season

    # PPR per game
    agg["ppr_per_game"] = agg["total_fantasy_points"] / agg["games"].replace(0, pd.NA)

    agg.to_parquet(path, index=False)
    return agg


def compute_snap_pct(season: int) -> pd.DataFrame:
    """
    Derive season-level average offensive snap percentage from weekly snap data.
    """
    cache_name = f"snap_pct_{season}"
    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)

    snaps = fetch_snap_counts(season)

    # Keep only offensive snap data
    needed = ["player", "pfr_player_id", "position", "team", "week", "season",
              "offense_snaps", "offense_pct"]
    snaps = snaps[[c for c in needed if c in snaps.columns]].copy()
    snaps = snaps[snaps["position"].isin(SKILL_POSITIONS)]

    agg = (
        snaps.groupby(["player", "pfr_player_id", "position", "team"])
        .agg(
            games=("week", "count"),
            total_offense_snaps=("offense_snaps", "sum"),
            avg_snap_pct=("offense_pct", "mean"),
        )
        .reset_index()
    )
    agg["season"] = season
    agg.to_parquet(path, index=False)
    return agg


def get_player_season_summary(player_name: str, season: int) -> Optional[dict]:
    """
    Convenience lookup: return a dict of key stats for a named player in a season.
    Used for verification and agent context.
    """
    ts = compute_target_share(season)
    # Case-insensitive partial match
    mask = ts["player_name"].str.contains(player_name, case=False, na=False)
    if mask.sum() == 0:
        return None
    row = ts[mask].iloc[0]
    return row.to_dict()


# ---------------------------------------------------------------------------
# Async wrappers (use run_in_executor to avoid blocking the event loop)
# ---------------------------------------------------------------------------

async def get_weekly_stats(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_weekly_stats, season)


async def get_seasonal_data(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_seasonal_data, season)


async def get_snap_counts(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_snap_counts, season)


async def get_schedules(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_schedules, season)


async def get_players() -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_players)


async def get_rosters(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_rosters, season)


async def get_ngs_data(stat_type: str, season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_ngs_data, stat_type, season)


async def get_injuries(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_injuries, season)


async def get_target_share(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, compute_target_share, season)


async def get_snap_pct(season: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, compute_snap_pct, season)


# ---------------------------------------------------------------------------
# Draft pick data
# ---------------------------------------------------------------------------

def fetch_nfl_draft_picks(year: int) -> pd.DataFrame:
    """
    Return the NFL draft class for a given year.
    Columns: player_name, position, round, pick_number, team, college, age_at_draft
    """
    return _load_or_fetch(
        f"draft_picks_{year}",
        lambda: nfl.import_draft_picks([year]),
    )


async def get_nfl_draft_picks(year: int) -> pd.DataFrame:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fetch_nfl_draft_picks, year)


# Approximate-value draft chart (pick_overall -> normalized 0-100 value)
# Values match stage-02-data-ingestion.md spec.
_AV_CHART: dict[int, float] = {
    1: 100, 2: 96, 3: 92, 4: 88, 5: 85, 6: 82, 7: 79, 8: 76, 9: 74, 10: 72,
    11: 70, 12: 68, 13: 66, 14: 64, 15: 62, 16: 60, 17: 58, 18: 56, 19: 55, 20: 54,
    21: 53, 22: 52, 23: 51, 24: 50, 25: 49, 26: 48, 27: 47, 28: 46, 29: 45, 30: 44,
    31: 43, 32: 48, 33: 47, 64: 28, 96: 16, 128: 9, 160: 5, 192: 3, 224: 2, 256: 1,
}


def get_draft_capital_value(draft_round: int, pick_overall: int) -> float:
    """
    Convert draft position to normalized 0-100 value using AV-based chart.
    Pick 1 overall = 100. Pick 256 = ~1.
    Interpolates linearly for picks not explicitly in the chart.
    draft_round is accepted for API symmetry; pick_overall is the canonical input.
    """
    if pick_overall in _AV_CHART:
        return float(_AV_CHART[pick_overall])
    # Linear interpolation between the two nearest bracketing chart entries
    keys = sorted(_AV_CHART.keys())
    for i in range(len(keys) - 1):
        lo, hi = keys[i], keys[i + 1]
        if lo < pick_overall < hi:
            lo_val = float(_AV_CHART[lo])
            hi_val = float(_AV_CHART[hi])
            frac = (pick_overall - lo) / (hi - lo)
            interpolated = lo_val + frac * (hi_val - lo_val)
            return float(int(interpolated * 10 + 0.5) / 10)  # manual round-to-1dp
    return max(1.0, 100.0 - (pick_overall * 0.38))


def get_capital_signal(capital_value: float) -> str:
    """
    Categorize draft capital into high/medium/low buckets.
    high  >= 70  → rounds 1-2
    medium >= 40 → rounds 3-4
    low         → rounds 5-7
    """
    if capital_value >= 70:
        return "high"
    if capital_value >= 40:
        return "medium"
    return "low"
