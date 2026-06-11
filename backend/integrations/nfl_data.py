"""
NFL data integration — wraps nfl_data_py with a parquet cache layer.

Sync functions (fetch_*) are for scripts.
Async functions (get_*) are for the agent pipeline / FastAPI.
Cache lives in data/cache/ (gitignored).
"""
from __future__ import annotations

import asyncio
import logging
import os
import pickle
import re
from pathlib import Path
from typing import Optional

import pandas as pd
import nfl_data_py as nfl

from backend.integrations import parquet_cache

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "data/cache"))
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}


# ---------------------------------------------------------------------------
# Name normalization utilities — shared by all agents
# ---------------------------------------------------------------------------

# Nickname → canonical name mapping for players known by alternate names.
# Keys and values should be lowercase.
_NICKNAME_ALIASES: dict[str, str] = {
    "hollywood brown": "marquise brown",
    "scotty miller": "scott miller",
    "mitch trubisky": "mitchell trubisky",
    "robby anderson": "chosen anderson",
    "willie snead": "willie snead iv",
}


def normalize_player_name(name: str) -> str:
    """
    Normalize player names for matching across data sources.
    Handles the most common NFL data name format differences:
      - Suffixes: Jr., Sr., II, III, IV
      - Double initials: D.K. → dk, A.J. → aj, J.K. → jk
      - Apostrophes: Ja'Marr → jamarr
      - Trailing/extra periods
      - Nickname aliases (Hollywood Brown → Marquise Brown)
    """
    if not name:
        return ""
    normalized = name.lower().strip()
    # Apply nickname aliases before further normalization
    if normalized in _NICKNAME_ALIASES:
        normalized = _NICKNAME_ALIASES[normalized]
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


def _cache_path(name: str) -> Path:
    return parquet_cache.cache_path(CACHE_DIR, name)


def _load_or_fetch(cache_name: str, fetch_fn) -> pd.DataFrame:
    return parquet_cache.load_or_fetch(CACHE_DIR, cache_name, fetch_fn)


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
    try:
        return _load_or_fetch(
            f"rosters_{season}",
            lambda: nfl.import_weekly_rosters([season]),
        )
    except Exception:
        # Current season rosters may not be published yet — fall back
        fallback = season - 1
        logger.warning("Rosters %d not available, falling back to %d", season, fallback)
        return _load_or_fetch(
            f"rosters_{fallback}",
            lambda: nfl.import_weekly_rosters([fallback]),
        )


def fetch_seasonal_rosters(season: int) -> pd.DataFrame:
    """Current roster data — uses import_seasonal_rosters (not weekly game rosters)."""
    return _load_or_fetch(
        f"seasonal_rosters_{season}",
        lambda: nfl.import_seasonal_rosters([season]),
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


def fetch_depth_charts(season: int) -> pd.DataFrame:
    """
    Fetch NFL depth charts for a season, filtered to latest date, offense only.
    Normalizes column names across different nfl_data_py schema versions:
      - 2024: club_code, depth_position, depth_team, full_name, week
      - 2025+: team, pos_abb, pos_rank, pos_grp, player_name, dt
    Returns DataFrame with columns: team, position, full_name, gsis_id, depth_rank.
    """
    cache_name = f"depth_charts_{season}"
    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)

    logger.info("Downloading depth charts for %d", season)
    try:
        raw = nfl.import_depth_charts([season])
    except Exception as exc:
        logger.warning("Depth charts unavailable for %d: %s", season, exc)
        return pd.DataFrame()

    if raw.empty:
        return pd.DataFrame()

    # --- Normalize column names across schema versions ---
    # Team column
    if "club_code" in raw.columns and "team" not in raw.columns:
        raw = raw.rename(columns={"club_code": "team"})

    # Position column: depth_position (2024) or pos_abb (2025+)
    if "position" not in raw.columns:
        if "depth_position" in raw.columns:
            raw = raw.rename(columns={"depth_position": "position"})
        elif "pos_abb" in raw.columns:
            raw = raw.rename(columns={"pos_abb": "position"})

    # Name column: full_name (2024) or player_name (2025+) → full_name
    if "full_name" not in raw.columns and "player_name" in raw.columns:
        raw = raw.rename(columns={"player_name": "full_name"})

    # Verify required columns exist
    if "position" not in raw.columns or "team" not in raw.columns:
        logger.warning("Depth charts %d: missing required columns (have: %s)",
                        season, list(raw.columns))
        return pd.DataFrame()

    # --- Filter to offense only ---
    if "depth_team" in raw.columns:
        raw = raw[raw["depth_team"].str.lower() == "offense"].copy()
    elif "pos_grp" in raw.columns:
        # 2025+ schema: pos_grp contains group names like "Base Offense"
        # Filter to offense by matching known offensive position abbreviations
        _OFFENSE_POS = {"QB", "RB", "WR", "TE", "LT", "LG", "C", "RG", "RT", "FB"}
        raw = raw[raw["position"].str.upper().isin(_OFFENSE_POS)].copy()

    # --- Filter to latest snapshot per team ---
    if "week" in raw.columns:
        max_week = raw.groupby("team")["week"].transform("max")
        raw = raw[raw["week"] == max_week].copy()
    elif "dt" in raw.columns:
        # 2025+ schema: dt is a datetime string, use latest date per team
        raw["_dt_parsed"] = pd.to_datetime(raw["dt"], errors="coerce")
        max_dt = raw.groupby("team")["_dt_parsed"].transform("max")
        raw = raw[raw["_dt_parsed"] == max_dt].copy()
        raw = raw.drop(columns=["_dt_parsed"])

    # --- Keep only skill positions ---
    raw = raw[raw["position"].str.upper().isin(SKILL_POSITIONS)].copy()
    if raw.empty:
        return pd.DataFrame()

    # Normalize position to uppercase
    raw["position"] = raw["position"].str.upper()

    # --- Compute depth rank ---
    if "pos_rank" in raw.columns:
        # 2025+ schema already has pos_rank
        raw["depth_rank"] = raw["pos_rank"].astype(int)
    else:
        # 2024 schema: compute from row order within (team, position)
        raw = raw.sort_values(["team", "position"]).copy()
        raw["depth_rank"] = raw.groupby(["team", "position"]).cumcount() + 1

    raw.to_parquet(path, index=False)
    return raw


def _normalize_team_abbr(abbr: str) -> str:
    """Normalize team abbreviations across data sources.
    ESPN depth charts use 'LA' for Rams; rosters use 'LA' or 'LAR'."""
    _ALIASES = {"LAR": "LA", "SL": "LA", "STL": "LA",
                "OAK": "LV", "SD": "LAC", "WSH": "WAS"}
    upper = str(abbr).upper()
    return _ALIASES.get(upper, upper)


def _filter_depth_charts_by_roster(
    depth_charts: pd.DataFrame, rosters: pd.DataFrame
) -> pd.DataFrame:
    """
    Remove depth chart entries where the player's roster team doesn't match
    the depth chart team.  ESPN's depth chart feed contains stale entries
    (e.g. Pacheco listed on DET when roster confirms he's on KC).

    Keeps rows where:
      1. gsis_id matches a roster row AND roster team == depth chart team, OR
      2. gsis_id is missing/NaN (can't verify — keep as-is)

    Players with a valid gsis_id whose roster team != depth chart team are dropped.
    """
    if depth_charts.empty or rosters.empty:
        return depth_charts

    if "gsis_id" not in depth_charts.columns:
        return depth_charts

    # Build gsis_id → most-recent roster team mapping
    if "player_id" not in rosters.columns or "team" not in rosters.columns:
        return depth_charts

    # Use the latest week per player to get current team
    roster_cols = rosters[["player_id", "team"]].copy()
    if "week" in rosters.columns:
        roster_cols["week"] = rosters["week"]
        latest = roster_cols.sort_values("week").drop_duplicates(
            subset=["player_id"], keep="last"
        )
    else:
        latest = roster_cols.drop_duplicates(subset=["player_id"], keep="last")

    gsis_to_team = dict(zip(latest["player_id"], latest["team"]))

    # Filter: keep row if no gsis_id OR roster team matches depth chart team
    def _keep(row):
        gsis = row.get("gsis_id")
        if pd.isna(gsis) or not gsis:
            return True  # Can't verify — keep
        roster_team = gsis_to_team.get(str(gsis))
        if roster_team is None:
            return True  # Not in roster data — keep (could be new signing)
        return _normalize_team_abbr(roster_team) == _normalize_team_abbr(row["team"])

    mask = depth_charts.apply(_keep, axis=1)
    filtered = depth_charts[mask].copy()
    dropped = len(depth_charts) - len(filtered)
    if dropped > 0:
        logger.info("Depth charts: removed %d stale entries via roster cross-ref", dropped)
    return filtered


def _compute_target_share_from_pbp(season: int) -> pd.DataFrame:
    """
    Fallback: compute target share stats from PBP data when weekly stats
    are unavailable (e.g. 2025 nflverse hasn't published player_stats yet).

    Uses compute_seasonal_stats_from_pbp() which is verified accurate,
    then transforms to match the compute_target_share() output schema.
    """
    logger.info("Computing target share from PBP fallback for %d", season)
    pbp = compute_seasonal_stats_from_pbp(season)

    # Filter to skill positions
    pbp = pbp[pbp["position"].isin(SKILL_POSITIONS)].copy()

    if pbp.empty:
        return pd.DataFrame()

    # Compute team-level targets for target share calculation
    team_targets = (
        pbp.groupby("recent_team")["targets"]
        .sum()
        .reset_index()
        .rename(columns={"targets": "team_targets"})
    )
    pbp = pbp.merge(team_targets, on="recent_team", how="left")
    pbp["target_share"] = pbp["targets"] / pbp["team_targets"].replace(0, pd.NA)

    # Rename columns to match compute_target_share() output schema
    agg = pbp.rename(columns={
        "player_display_name": "player_name",
        "targets": "total_targets",
        "receptions": "total_receptions",
        "receiving_yards": "total_rec_yards",
        "receiving_tds": "total_rec_tds",
        "rush_attempts": "total_carries",
        "rushing_yards": "total_rush_yards",
        "rushing_tds": "total_rush_tds",
        "fantasy_points_ppr": "total_fantasy_points",
        "target_share": "avg_target_share",
    })

    # Air yards not available in PBP fallback — set to 0
    agg["total_air_yards"] = 0.0
    agg["avg_air_yards_share"] = 0.0

    # PPR per game
    agg["ppr_per_game"] = agg["total_fantasy_points"] / agg["games"].replace(0, pd.NA)

    # Keep only the columns that compute_target_share() returns
    keep = [
        "player_id", "player_name", "recent_team", "position",
        "games", "total_targets", "total_receptions", "total_rec_yards",
        "total_rec_tds", "avg_target_share", "total_air_yards",
        "avg_air_yards_share", "total_carries", "total_rush_yards",
        "total_rush_tds", "total_fantasy_points", "season", "ppr_per_game",
    ]
    return agg[[c for c in keep if c in agg.columns]]


def compute_target_share(season: int) -> pd.DataFrame:
    """
    Derive per-player target share and air yards share from weekly data.
    Returns one row per player with season-level averages.

    Falls back to PBP-derived stats when weekly data is unavailable
    (e.g. 2025 where nflverse hasn't published player_stats yet).
    """
    cache_name = f"target_share_{season}"
    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)

    try:
        weekly = fetch_weekly_stats(season)
    except Exception:
        logger.warning(
            "Weekly stats unavailable for %d — falling back to PBP", season,
        )
        agg = _compute_target_share_from_pbp(season)
        if not agg.empty:
            agg.to_parquet(path, index=False)
        return agg

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


async def get_seasonal_rosters(season: int) -> pd.DataFrame:
    return await asyncio.to_thread(fetch_seasonal_rosters, season)


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
# PBP fallback — compute seasonal stats when player_stats file is missing
# ---------------------------------------------------------------------------


def compute_seasonal_stats_from_pbp(
    season: int,
    scoring: str = "ppr",
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Compute PPR fantasy points from play-by-play data.

    Used as fallback when nflverse hasn't published the pre-computed
    player_stats_{year}.parquet (e.g. 2025).

    Verified accurate for 2025:
      CMC: 414.6, Allen: 378.6, Nacua: 377.0
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"seasonal_pbp_{season}.pkl"
    if use_cache and cache_file.exists():
        logger.info("Loading %d PBP stats from cache", season)
        with open(cache_file, "rb") as f:
            return pickle.load(f)

    logger.info("Computing %d stats from PBP data...", season)

    try:
        pbp = nfl.import_pbp_data([season])
    except Exception as exc:
        logger.warning("PBP data unavailable for %d: %s", season, exc)
        return pd.DataFrame()

    if pbp.empty or "season_type" not in pbp.columns:
        logger.warning("PBP data for %d is empty or missing season_type", season)
        return pd.DataFrame()

    pbp = pbp[pbp["season_type"] == "REG"].copy()

    SCORING_MAP = {"ppr": 1.0, "half_ppr": 0.5, "standard": 0.0}
    rec_pts = SCORING_MAP.get(scoring, 1.0)

    player_stats: dict[str, dict] = {}

    def _safe_int(val, default=0) -> int:
        """Convert value to int, treating NaN/None as default."""
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return int(val)

    def _safe_float(val, default=0.0) -> float:
        """Convert value to float, treating NaN/None as default."""
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return float(val)

    def _get(pid: str, pname: str) -> dict:
        if pid not in player_stats:
            player_stats[pid] = {
                "player_id": pid,
                "player_name": pname,
                "games": set(),
                "receptions": 0,
                "receiving_yards": 0,
                "receiving_tds": 0,
                "rush_attempts": 0,
                "rushing_yards": 0,
                "rushing_tds": 0,
                "passing_yards": 0,
                "passing_tds": 0,
                "interceptions": 0,
                "targets": 0,
                "fumbles_lost": 0,
                "fantasy_points_ppr": 0.0,
            }
        return player_stats[pid]

    for _, play in pbp.iterrows():
        game_id = play.get("game_id", "")

        # --- Receiving ---
        rec_id = play.get("receiver_player_id")
        if rec_id and pd.notna(rec_id):
            rec_name = play.get("receiver_player_name", "")
            p = _get(rec_id, rec_name)
            p["games"].add(game_id)
            if _safe_int(play.get("pass_attempt")) == 1:
                p["targets"] += 1
            if _safe_int(play.get("complete_pass")) == 1:
                p["receptions"] += 1
                yards = _safe_float(play.get("receiving_yards"))
                p["receiving_yards"] += yards
                p["fantasy_points_ppr"] += rec_pts + yards * 0.1
                if _safe_int(play.get("touchdown")) == 1:
                    p["receiving_tds"] += 1
                    p["fantasy_points_ppr"] += 6.0

        # --- Rushing ---
        rush_id = play.get("rusher_player_id")
        if rush_id and pd.notna(rush_id):
            rush_name = play.get("rusher_player_name", "")
            p = _get(rush_id, rush_name)
            p["games"].add(game_id)
            p["rush_attempts"] += 1
            yards = _safe_float(play.get("rushing_yards"))
            p["rushing_yards"] += yards
            p["fantasy_points_ppr"] += yards * 0.1
            if _safe_int(play.get("touchdown")) == 1:
                p["rushing_tds"] += 1
                p["fantasy_points_ppr"] += 6.0

        # --- Passing ---
        pass_id = play.get("passer_player_id")
        if pass_id and pd.notna(pass_id):
            pass_name = play.get("passer_player_name", "")
            p = _get(pass_id, pass_name)
            p["games"].add(game_id)
            yards = _safe_float(play.get("passing_yards"))
            p["passing_yards"] += yards
            p["fantasy_points_ppr"] += yards * 0.04
            if _safe_int(play.get("pass_touchdown")) == 1:
                p["passing_tds"] += 1
                p["fantasy_points_ppr"] += 4.0
            if _safe_int(play.get("interception")) == 1:
                p["interceptions"] += 1
                p["fantasy_points_ppr"] -= 2.0

        # --- Fumbles lost ---
        if _safe_int(play.get("fumble_lost")) == 1:
            fumbler_id = play.get("fumbled_1_player_id")
            if fumbler_id and pd.notna(fumbler_id):
                fumbler_name = play.get("fumbled_1_player_name", "")
                p = _get(fumbler_id, fumbler_name)
                p["fumbles_lost"] += 1
                p["fantasy_points_ppr"] -= 2.0

    # Convert to DataFrame
    rows = []
    for stats in player_stats.values():
        games = len(stats["games"])
        if games == 0:
            continue
        rows.append({
            "player_id": stats["player_id"],
            "player_display_name": stats["player_name"],
            "season": season,
            "games": games,
            "receptions": stats["receptions"],
            "receiving_yards": stats["receiving_yards"],
            "receiving_tds": stats["receiving_tds"],
            "rush_attempts": stats["rush_attempts"],
            "rushing_yards": stats["rushing_yards"],
            "rushing_tds": stats["rushing_tds"],
            "passing_yards": stats["passing_yards"],
            "passing_tds": stats["passing_tds"],
            "interceptions": stats["interceptions"],
            "targets": stats["targets"],
            "fumbles_lost": stats["fumbles_lost"],
            "fantasy_points_ppr": round(stats["fantasy_points_ppr"], 2),
        })

    df = pd.DataFrame(rows)

    # Add position from seasonal rosters
    try:
        rosters = nfl.import_seasonal_rosters([season])
        # Column is "team" in seasonal rosters, normalize to "recent_team"
        pos_cols = rosters[["player_id", "position", "team"]].copy()
        pos_cols = pos_cols.rename(columns={"team": "recent_team"})
        pos_cols = pos_cols.drop_duplicates("player_id")
        df = df.merge(pos_cols, on="player_id", how="left")
    except Exception as exc:
        logger.warning("Could not join roster positions for %d: %s", season, exc)
        df["position"] = None
        df["recent_team"] = None

    logger.info("Computed PBP stats for %d players in season %d", len(df), season)

    # Cache result (skip if use_cache=False to avoid test pollution)
    if use_cache:
        with open(cache_file, "wb") as f:
            pickle.dump(df, f)

    return df


def get_seasonal_stats(season: int, scoring: str = "ppr") -> pd.DataFrame:
    """
    Get seasonal fantasy stats for a given year.

    Tries nfl_data_py import_weekly_data() first (pre-computed parquet).
    Falls back to PBP computation if the file doesn't exist (e.g. 2025).
    """
    try:
        cols = [
            "player_id", "player_display_name", "position",
            "recent_team", "fantasy_points_ppr", "season_type",
        ]
        weekly = nfl.import_weekly_data([season], cols)
        if len(weekly) > 0:
            logger.info("Loaded %d weekly stats from nflverse for season %d", len(weekly), season)
            # Aggregate to seasonal
            weekly = weekly[
                (weekly["season_type"] == "REG")
                & (weekly["position"].isin(["QB", "RB", "WR", "TE"]))
            ]
            seasonal = (
                weekly.groupby(["player_id", "player_display_name", "position", "recent_team"])
                .agg(
                    games=("fantasy_points_ppr", "count"),
                    fantasy_points_ppr=("fantasy_points_ppr", "sum"),
                )
                .reset_index()
            )
            seasonal = seasonal.sort_values("games", ascending=False).drop_duplicates("player_id")
            if "player_display_name" in seasonal.columns:
                seasonal = seasonal.rename(columns={"player_display_name": "player_name"})
            return seasonal
        raise ValueError("Empty dataframe from import_weekly_data")
    except Exception as exc:
        logger.info("import_weekly_data(%d) failed: %s — falling back to PBP", season, exc)
        result = compute_seasonal_stats_from_pbp(season, scoring)
        if "player_display_name" in result.columns:
            result = result.rename(columns={"player_display_name": "player_name"})
        return result


# ---------------------------------------------------------------------------
# Draft pick data
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# QB and O-line aggregation functions
# ---------------------------------------------------------------------------


def compute_qb_season_stats(season: int) -> pd.DataFrame:
    """
    Per-QB season aggregates from weekly stats + NGS passing data.

    Returns one row per QB with passing, rushing, and efficiency metrics.
    Cached as parquet.  Falls back to PBP when nflverse weekly stats
    unavailable (e.g. 2025).
    """
    cache_name = f"qb_season_{season}"
    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)

    try:
        weekly = fetch_weekly_stats(season)
        if weekly is None or len(weekly) == 0:
            raise ValueError(f"No weekly stats for {season}")
    except Exception as exc:
        logger.warning(
            "compute_qb_season_stats(%d) weekly stats failed: %s — falling back to PBP",
            season, exc,
        )
        return _compute_qb_stats_from_pbp(season)

    # QB rows, regular season only
    qbs = weekly[weekly["position"] == "QB"].copy()
    if "season_type" in qbs.columns:
        qbs = qbs[qbs["season_type"] == "REG"].copy()

    if qbs.empty:
        empty = pd.DataFrame()
        empty.to_parquet(path, index=False)
        return empty

    # Season-level aggregation per QB
    agg = (
        qbs.groupby(["player_id", "player_name", "recent_team"])
        .agg(
            games=("week", "count"),
            completions=("completions", "sum"),
            attempts=("attempts", "sum"),
            passing_yards=("passing_yards", "sum"),
            passing_tds=("passing_tds", "sum"),
            interceptions=("interceptions", "sum"),
            sacks=("sacks", "sum"),
            rushing_yards=("rushing_yards", "sum"),
            rushing_tds=("rushing_tds", "sum"),
            carries=("carries", "sum"),
            fantasy_points_ppr=("fantasy_points_ppr", "sum"),
        )
        .reset_index()
    )

    # Derived metrics (use float division to avoid NAType round issues)
    agg["completion_pct"] = (
        agg["completions"].astype(float) / agg["attempts"].replace(0, float("nan"))
    ).round(3)
    agg["ppr_per_game"] = (
        agg["fantasy_points_ppr"].astype(float) / agg["games"].replace(0, float("nan"))
    ).round(1)
    agg["rushing_yards_per_game"] = (
        agg["rushing_yards"].astype(float) / agg["games"].replace(0, float("nan"))
    ).round(1)
    agg["season"] = season

    # Merge NGS passing data (CPOE, time_to_throw, aggressiveness)
    try:
        ngs = fetch_ngs_data("passing", season)
        if not ngs.empty:
            # Filter to season-level (week==0) REG rows
            ngs_season = ngs[
                (ngs["season_type"] == "REG") & (ngs["week"] == 0)
            ].copy()
            if not ngs_season.empty:
                ngs_cols = ngs_season[["player_gsis_id", "completion_percentage_above_expectation",
                                       "avg_time_to_throw", "aggressiveness"]].copy()
                ngs_cols = ngs_cols.rename(columns={
                    "player_gsis_id": "player_id",
                    "completion_percentage_above_expectation": "cpoe",
                })
                agg = agg.merge(ngs_cols, on="player_id", how="left")
    except Exception as exc:
        logger.warning("Could not merge NGS passing data for %d: %s", season, exc)

    # Ensure NGS columns exist even if merge failed
    for col in ("cpoe", "avg_time_to_throw", "aggressiveness"):
        if col not in agg.columns:
            agg[col] = pd.NA

    agg.to_parquet(path, index=False)
    return agg


def _compute_qb_stats_from_pbp(season: int) -> pd.DataFrame:
    """
    Compute QB season stats from PBP data.

    Used when nflverse hasn't published weekly stats (e.g. 2025).
    Returns DataFrame with columns matching compute_qb_season_stats()
    output so downstream code needs no changes.
    """
    all_stats = compute_seasonal_stats_from_pbp(season, scoring="ppr")

    if all_stats is None or len(all_stats) == 0:
        return pd.DataFrame()

    # Filter to QBs: players with >500 passing yards are QBs
    # (filters out WR/RB trick-play passes).
    # compute_seasonal_stats_from_pbp already merges position from rosters.
    qbs = all_stats[all_stats["passing_yards"] > 500].copy()

    # Rename columns to match compute_qb_season_stats() output
    col_map = {
        "player_display_name": "player_name",
        "rush_attempts": "carries",
    }
    qbs = qbs.rename(columns={k: v for k, v in col_map.items() if k in qbs.columns})

    # Derive columns that PBP doesn't have natively
    qbs["ppr_per_game"] = (
        qbs["fantasy_points_ppr"].astype(float) / qbs["games"].replace(0, float("nan"))
    ).round(1)
    qbs["rushing_yards_per_game"] = (
        qbs["rushing_yards"].astype(float) / qbs["games"].replace(0, float("nan"))
    ).round(1)

    # PBP doesn't track completions/attempts/sacks separately for passers;
    # set to NA so downstream code doesn't crash on missing columns
    for col in ("completions", "attempts", "completion_pct", "sacks",
                "cpoe", "avg_time_to_throw", "aggressiveness"):
        if col not in qbs.columns:
            qbs[col] = pd.NA

    logger.info(
        "Computed QB stats from PBP for %d QBs in season %d", len(qbs), season
    )
    return qbs


def _compute_oline_from_pbp(season: int) -> pd.DataFrame:
    """
    Compute team sack rate from PBP data.
    Used when nflverse weekly stats unavailable (e.g. 2025).
    """
    cache_name = f"oline_stats_{season}_pbp"
    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)

    try:
        # Load full PBP — do NOT pass columns= kwarg, it triggers a
        # KeyError in nfl_data_py for some seasons (e.g. 2025).
        pbp = nfl.import_pbp_data([season])
        if pbp.empty or "season_type" not in pbp.columns:
            logger.warning("PBP oline: no data for %d", season)
            return pd.DataFrame()
        needed = ["season_type", "posteam", "pass_attempt", "sack"]
        pbp = pbp[needed]
        pbp = pbp[pbp["season_type"] == "REG"]

        team_stats = pbp.groupby("posteam").agg(
            total_attempts=("pass_attempt", "sum"),
            total_sacks=("sack", "sum"),
        ).reset_index().rename(columns={"posteam": "team"})

        team_stats["total_dropbacks"] = (
            team_stats["total_attempts"] + team_stats["total_sacks"]
        )
        team_stats["sack_rate"] = (
            team_stats["total_sacks"].astype(float)
            / team_stats["total_dropbacks"].replace(0, float("nan"))
        ).round(4)
        team_stats["season"] = season
        # NGS time_to_throw not available in PBP
        team_stats["avg_time_to_throw"] = pd.NA

        team_stats.to_parquet(path, index=False)
        logger.info(
            "Computed oline stats from PBP for %d teams in season %d",
            len(team_stats), season,
        )
        return team_stats

    except Exception as e:
        logger.error("PBP oline fallback failed for %d: %s", season, e)
        return pd.DataFrame()


def compute_team_oline_stats(season: int) -> pd.DataFrame:
    """
    Per-team O-line metrics: sack_rate and avg_time_to_throw.

    Sack rate = total_sacks / total_dropbacks (pass attempts + sacks).
    Time to throw from NGS passing data aggregated to team level.
    Cached as parquet.
    """
    cache_name = f"oline_stats_{season}"
    path = _cache_path(cache_name)
    if path.exists():
        return pd.read_parquet(path)

    try:
        weekly = fetch_weekly_stats(season)
        if weekly is None or len(weekly) == 0:
            raise ValueError(f"No weekly stats for {season}")
    except Exception as exc:
        logger.warning(
            "compute_team_oline_stats(%d) weekly stats failed: %s — falling back to PBP",
            season, exc,
        )
        return _compute_oline_from_pbp(season)

    # QB rows, regular season only
    qbs = weekly[weekly["position"] == "QB"].copy()
    if "season_type" in qbs.columns:
        qbs = qbs[qbs["season_type"] == "REG"].copy()

    if qbs.empty:
        empty = pd.DataFrame()
        empty.to_parquet(path, index=False)
        return empty

    # Aggregate sacks/attempts per team
    team_agg = (
        qbs.groupby("recent_team")
        .agg(
            total_attempts=("attempts", "sum"),
            total_sacks=("sacks", "sum"),
        )
        .reset_index()
        .rename(columns={"recent_team": "team"})
    )

    # Dropbacks = attempts + sacks (sacks don't count as pass attempts in weekly)
    team_agg["total_dropbacks"] = team_agg["total_attempts"] + team_agg["total_sacks"]
    team_agg["sack_rate"] = (
        team_agg["total_sacks"].astype(float) / team_agg["total_dropbacks"].replace(0, float("nan"))
    ).round(4)
    team_agg["season"] = season

    # Merge team-level avg_time_to_throw from NGS
    try:
        ngs = fetch_ngs_data("passing", season)
        if not ngs.empty:
            ngs_season = ngs[
                (ngs["season_type"] == "REG") & (ngs["week"] == 0)
            ].copy()
            if not ngs_season.empty and "avg_time_to_throw" in ngs_season.columns:
                # Weight by attempts for team-level average
                ngs_season = ngs_season[["team_abbr", "avg_time_to_throw", "attempts"]].copy()
                ngs_season["weighted_ttt"] = ngs_season["avg_time_to_throw"] * ngs_season["attempts"]
                team_ttt = (
                    ngs_season.groupby("team_abbr")
                    .agg(total_weighted_ttt=("weighted_ttt", "sum"), total_att=("attempts", "sum"))
                    .reset_index()
                )
                team_ttt["avg_time_to_throw"] = (
                    team_ttt["total_weighted_ttt"].astype(float) / team_ttt["total_att"].replace(0, float("nan"))
                ).round(3)
                team_ttt = team_ttt[["team_abbr", "avg_time_to_throw"]].rename(
                    columns={"team_abbr": "team"}
                )
                team_agg = team_agg.merge(team_ttt, on="team", how="left")
    except Exception as exc:
        logger.warning("Could not merge NGS time_to_throw for %d: %s", season, exc)

    if "avg_time_to_throw" not in team_agg.columns:
        team_agg["avg_time_to_throw"] = pd.NA

    team_agg.to_parquet(path, index=False)
    return team_agg


async def get_qb_season_stats(season: int) -> pd.DataFrame:
    """Async wrapper for compute_qb_season_stats."""
    return await asyncio.to_thread(compute_qb_season_stats, season)


async def get_team_oline_stats(season: int) -> pd.DataFrame:
    """Async wrapper for compute_team_oline_stats."""
    return await asyncio.to_thread(compute_team_oline_stats, season)


# ---------------------------------------------------------------------------
# NflDataWarehouse — single source of truth for all pipeline data
# ---------------------------------------------------------------------------


class NflDataWarehouse:
    """
    All NFL data needed by the pipeline, built once before any agents run.

    In May 2026 with analysis_seasons=[2023,2024,2025]:
      - 2023: nflverse parquet
      - 2024: nflverse parquet
      - 2025: PBP fallback (handled transparently by each underlying function)

    All agents receive this object and read from it.
    No agent fetches data independently.
    """

    def __init__(
        self,
        analysis_seasons: list[int],
        current_season: int,
        analysis_year: int,
    ):
        self.analysis_seasons = analysis_seasons
        self.current_season = current_season
        self.analysis_year = analysis_year

        # Player stats by season
        self.seasonal_stats: dict[int, pd.DataFrame] = {}
        self.target_share: dict[int, pd.DataFrame] = {}
        self.qb_stats: dict[int, pd.DataFrame] = {}

        # Team stats by season
        self.oline_stats: dict[int, pd.DataFrame] = {}
        self.def_grades: dict[int, pd.DataFrame] = {}

        # Injury data by season
        self.injuries: dict[int, pd.DataFrame] = {}

        # Current season infrastructure
        self.rosters: pd.DataFrame = pd.DataFrame()
        self.seasonal_rosters: pd.DataFrame = pd.DataFrame()
        self.prev_rosters: pd.DataFrame = pd.DataFrame()

        # Upcoming season schedule
        self.schedule: pd.DataFrame = pd.DataFrame()
        self.schedule_year: int = analysis_year

        # Supplementary data (player_profiles agent)
        self.ngs_receiving: dict[int, pd.DataFrame] = {}
        self.ngs_rushing: dict[int, pd.DataFrame] = {}
        self.ngs_passing: dict[int, pd.DataFrame] = {}
        self.snap_pct: dict[int, pd.DataFrame] = {}

        # Depth chart data (current season)
        self.depth_charts: dict[int, pd.DataFrame] = {}

    @classmethod
    def build(cls) -> "NflDataWarehouse":
        """
        Build the warehouse. Called once at the start of the pipeline.
        Logs clearly what loaded and what failed.

        Loads 6 seasons by default. Agents that only need 3 seasons
        use the 3 most recent. Per-player baseline computation uses
        however many seasons the player's career allows.
        """
        from backend.utils.seasons import (
            get_analysis_seasons,
            get_current_season,
            get_analysis_year,
        )

        wh = cls(
            analysis_seasons=get_analysis_seasons(6),
            current_season=get_current_season(),
            analysis_year=get_analysis_year(),
        )

        logger.info(
            "Building NflDataWarehouse | analysis=%s current=%d year=%d",
            wh.analysis_seasons, wh.current_season, wh.analysis_year,
        )

        for season in wh.analysis_seasons:
            wh._load_season(season)

        wh._load_infrastructure()
        wh._load_schedule()
        wh._load_supplements()

        logger.info("NflDataWarehouse ready.")
        return wh

    def _load_season(self, season: int) -> None:
        """Load all data for one completed season. Each function handles its own fallback."""
        logger.info("Loading season %d...", season)

        self.seasonal_stats[season] = get_seasonal_stats(season)
        logger.info("  %d seasonal_stats: %d players", season, len(self.seasonal_stats[season]))

        self.target_share[season] = self._load_target_share(season)
        logger.info("  %d target_share: %d players", season, len(self.target_share[season]))

        self.qb_stats[season] = compute_qb_season_stats(season)
        logger.info("  %d qb_stats: %d QBs", season, len(self.qb_stats[season]))

        self.oline_stats[season] = compute_team_oline_stats(season)
        logger.info("  %d oline_stats: %d teams", season, len(self.oline_stats[season]))

        # Defensive grades — fall back through seasons
        from backend.agents.schedule import compute_def_grades
        for yr in (season, season - 1):
            try:
                weekly = fetch_weekly_stats(yr)
                grades = compute_def_grades(weekly)
                if not grades.empty:
                    self.def_grades[season] = grades
                    if yr != season:
                        logger.warning("  %d def_grades: using %d as proxy", season, yr)
                    else:
                        logger.info("  %d def_grades: %d teams", season, len(grades))
                    break
            except Exception:
                pass
        else:
            self.def_grades[season] = pd.DataFrame()
            logger.warning("  %d def_grades: unavailable", season)

        # Injury reports — Sleeper for current season, nfl_data_py for historical
        if season == self.current_season:
            try:
                from backend.integrations.sleeper import get_sleeper_injuries
                inj = get_sleeper_injuries()
                if not inj.empty:
                    self.injuries[season] = inj
                    logger.info("  %d injuries from Sleeper: %d rows", season, len(inj))
                else:
                    raise ValueError("Sleeper injuries empty")
            except Exception as e:
                logger.warning("  %d Sleeper injuries failed, trying nfl_data_py: %s", season, e)
                try:
                    self.injuries[season] = fetch_injuries(season)
                    logger.info("  %d injuries from nfl_data_py: %d rows", season, len(self.injuries[season]))
                except Exception as e2:
                    self.injuries[season] = pd.DataFrame()
                    logger.warning("  %d injuries unavailable: %s", season, e2)
        else:
            try:
                self.injuries[season] = fetch_injuries(season)
                logger.info("  %d injuries: %d rows", season, len(self.injuries[season]))
            except Exception as e:
                self.injuries[season] = pd.DataFrame()
                logger.warning("  %d injuries unavailable: %s", season, e)

    @staticmethod
    def _load_target_share(season: int) -> pd.DataFrame:
        """Load target share: Sleeper primary, nfl_data_py air yards overlay."""
        from backend.integrations.sleeper import compute_sleeper_target_share

        try:
            sleeper_ts = compute_sleeper_target_share(season)
        except Exception as exc:
            logger.warning("Sleeper target_share %d failed: %s", season, exc)
            sleeper_ts = pd.DataFrame()

        nfl_ts = compute_target_share(season)

        if sleeper_ts.empty:
            return nfl_ts

        # Overlay nfl_data_py air yards onto Sleeper base where gsis_id matches
        if not nfl_ts.empty and "player_id" in sleeper_ts.columns and "player_id" in nfl_ts.columns:
            air = nfl_ts[["player_id", "avg_air_yards_share", "total_air_yards"]].dropna(
                subset=["player_id"]
            )
            if not air.empty:
                merged = sleeper_ts.merge(air, on="player_id", how="left", suffixes=("", "_nfl"))
                for col in ("avg_air_yards_share", "total_air_yards"):
                    nfl_col = f"{col}_nfl"
                    if nfl_col in merged.columns:
                        merged[col] = merged[nfl_col].combine_first(merged[col])
                        merged.drop(columns=[nfl_col], inplace=True)
                return merged

        return sleeper_ts

    def _load_infrastructure(self) -> None:
        """Rosters + depth charts + injuries for current season.

        Current rosters from Sleeper API (accurate, daily-updated, ~3900 active skill players).
        Previous rosters from nfl_data_py (2025 season baseline for departure/arrival detection).
        Depth charts and injuries also from Sleeper.
        """
        try:
            from backend.integrations.sleeper import fetch_sleeper_players
            self.rosters = fetch_sleeper_players()
            # Add backward-compat alias: team_systems uses "player_name"
            if "full_name" in self.rosters.columns and "player_name" not in self.rosters.columns:
                self.rosters["player_name"] = self.rosters["full_name"]
            logger.info("Rosters (Sleeper): %d active skill players", len(self.rosters))
        except Exception as e:
            logger.warning("Sleeper rosters failed, falling back to nfl_data_py: %s", e)
            try:
                self.rosters = fetch_rosters(self.current_season)
                logger.info("Rosters fallback (nfl_data_py %d): %d rows", self.current_season, len(self.rosters))
            except Exception as e2:
                logger.warning("Rosters %d failed: %s", self.current_season, e2)

        try:
            self.seasonal_rosters = fetch_seasonal_rosters(self.current_season)
            logger.info("Seasonal rosters %d: %d rows", self.current_season, len(self.seasonal_rosters))
        except Exception as e:
            logger.warning("Seasonal rosters %d failed: %s", self.current_season, e)

        # Previous season rosters — needed by roster_changes for departure/arrival detection
        prev = self.current_season - 1
        try:
            self.prev_rosters = fetch_rosters(prev)
            logger.info("Previous rosters %d: %d rows", prev, len(self.prev_rosters))
        except Exception as e:
            logger.warning("Previous rosters %d failed: %s", prev, e)

        # Depth charts from Sleeper (primary) with nfl_data_py fallback
        self._load_depth_charts()

    def _load_depth_charts(self) -> None:
        """Load depth charts from Sleeper API. Fall back to nfl_data_py if Sleeper fails."""
        try:
            from backend.integrations.sleeper import get_sleeper_depth_charts
            dc = get_sleeper_depth_charts()
            if not dc.empty:
                # Normalize column names to match warehouse schema
                col_map = {}
                if "pos_abb" in dc.columns and "position" not in dc.columns:
                    col_map["pos_abb"] = "position"
                if "pos_rank" in dc.columns and "depth_rank" not in dc.columns:
                    col_map["pos_rank"] = "depth_rank"
                if "player_name" in dc.columns and "full_name" not in dc.columns:
                    col_map["player_name"] = "full_name"
                if col_map:
                    dc = dc.rename(columns=col_map)
                self.depth_charts[self.current_season] = dc
                logger.info("Depth charts from Sleeper: %d entries", len(dc))
                return
        except Exception as e:
            logger.warning("Sleeper depth charts failed: %s", e)

        # Fallback to nfl_data_py
        raw_dc = pd.DataFrame()
        for dc_year in (self.current_season, self.current_season - 1):
            try:
                raw_dc = fetch_depth_charts(dc_year)
                if not raw_dc.empty:
                    logger.info("Depth charts fallback (nfl_data_py %d): %d entries",
                                dc_year, len(raw_dc))
                    break
            except Exception as e:
                logger.warning("Depth charts %d failed: %s", dc_year, e)

        if not raw_dc.empty:
            dc = _filter_depth_charts_by_roster(raw_dc, self.rosters)
            self.depth_charts[self.current_season] = dc
            logger.info("Depth charts after roster filter: %d entries", len(dc))

    def _load_schedule(self) -> None:
        """Schedule for upcoming season."""
        for yr in (self.analysis_year, self.current_season, self.current_season - 1):
            try:
                df = fetch_schedules(yr)
                reg = df[df["game_type"] == "REG"] if "game_type" in df.columns else df
                if not reg.empty:
                    self.schedule = reg.copy()
                    self.schedule_year = yr
                    logger.info("Schedule %d loaded", yr)
                    return
            except Exception:
                pass
        logger.error("No schedule data available")

    def _load_supplements(self) -> None:
        """NGS and snap pct — used by player_profiles agent only."""
        from backend.agents.player_profiles import PlayerProfilesAgent

        for season in self.analysis_seasons:
            # NGS receiving
            try:
                raw = fetch_ngs_data("receiving", season)
                self.ngs_receiving[season] = PlayerProfilesAgent._aggregate_ngs(
                    raw, ["avg_separation", "avg_yac_above_expectation"]
                )
                logger.info("  %d ngs_receiving: %d players", season, len(self.ngs_receiving[season]))
            except Exception as exc:
                logger.warning("  %d ngs_receiving unavailable: %s", season, exc)

            # NGS rushing
            try:
                raw = fetch_ngs_data("rushing", season)
                self.ngs_rushing[season] = PlayerProfilesAgent._aggregate_ngs(
                    raw, ["rush_yards_over_expected_per_att", "rush_pct_over_expected"]
                )
                logger.info("  %d ngs_rushing: %d players", season, len(self.ngs_rushing[season]))
            except Exception as exc:
                logger.warning("  %d ngs_rushing unavailable: %s", season, exc)

            # NGS passing
            try:
                raw = fetch_ngs_data("passing", season)
                self.ngs_passing[season] = PlayerProfilesAgent._aggregate_ngs(
                    raw, ["completion_percentage_above_expectation", "avg_time_to_throw", "aggressiveness"]
                )
                logger.info("  %d ngs_passing: %d players", season, len(self.ngs_passing[season]))
            except Exception as exc:
                logger.warning("  %d ngs_passing unavailable: %s", season, exc)

        # Snap pct for current season
        try:
            self.snap_pct[self.current_season] = compute_snap_pct(self.current_season)
            logger.info("Snap pct %d: %d players", self.current_season, len(self.snap_pct[self.current_season]))
        except Exception as exc:
            logger.warning("Snap pct %d failed: %s", self.current_season, exc)

    # ----------------------------------------------------------
    # Clean accessors — return empty df, never raise
    # ----------------------------------------------------------

    def get_seasonal_stats(self, season: int) -> pd.DataFrame:
        return self.seasonal_stats.get(season, pd.DataFrame())

    def get_target_share(self, season: int) -> pd.DataFrame:
        return self.target_share.get(season, pd.DataFrame())

    def get_qb_stats(self, season: int) -> pd.DataFrame:
        return self.qb_stats.get(season, pd.DataFrame())

    def get_oline_stats(self, season: int) -> pd.DataFrame:
        return self.oline_stats.get(season, pd.DataFrame())

    def get_def_grades(self, season: int) -> pd.DataFrame:
        return self.def_grades.get(season, pd.DataFrame())

    def get_injuries(self, season: int) -> pd.DataFrame:
        return self.injuries.get(season, pd.DataFrame())

    def get_most_recent_def_grades(self) -> pd.DataFrame:
        for season in sorted(self.def_grades.keys(), reverse=True):
            if not self.def_grades[season].empty:
                return self.def_grades[season]
        return pd.DataFrame()

    def get_ngs_receiving(self, season: int) -> pd.DataFrame:
        return self.ngs_receiving.get(season, pd.DataFrame())

    def get_ngs_rushing(self, season: int) -> pd.DataFrame:
        return self.ngs_rushing.get(season, pd.DataFrame())

    def get_ngs_passing(self, season: int) -> pd.DataFrame:
        return self.ngs_passing.get(season, pd.DataFrame())

    def get_snap_pct(self, season: int) -> pd.DataFrame:
        return self.snap_pct.get(season, pd.DataFrame())

    def get_depth_chart(self, season: int) -> pd.DataFrame:
        """Return depth chart DataFrame for a season, or empty DataFrame."""
        return self.depth_charts.get(season, pd.DataFrame())

    def get_starter(self, team: str, position: str, season: int | None = None) -> dict | None:
        """
        Return the depth chart starter (rank=1) at a position for a team.
        Returns dict with keys: name, gsis_id, depth_rank.
        Returns None if no depth chart data or no starter found.
        """
        s = season or self.current_season
        dc = self.depth_charts.get(s, pd.DataFrame())
        if dc.empty:
            return None

        team_norm = _normalize_team_abbr(team)
        pos_upper = position.upper()

        mask = (
            (dc["team"].apply(_normalize_team_abbr) == team_norm)
            & (dc["position"].str.upper() == pos_upper)
            & (dc["depth_rank"] == 1)
        )
        starters = dc[mask]
        if starters.empty:
            return None

        row = starters.iloc[0]
        name_col = next(
            (c for c in ("full_name", "player_name") if c in dc.columns),
            None,
        )
        return {
            "name": str(row[name_col]) if name_col else "",
            "gsis_id": str(row["gsis_id"]) if "gsis_id" in dc.columns and pd.notna(row.get("gsis_id")) else None,
            "depth_rank": 1,
        }

    def get_player_depth_rank(
        self,
        gsis_id: str = "",
        season: int | None = None,
        *,
        sleeper_id: str = "",
        name: str = "",
    ) -> int | None:
        """
        Return depth chart rank for a player.
        Tries sleeper_id first (best coverage), then gsis_id, then name.
        1=starter, 2=backup, etc. Returns None if not found.
        """
        s = season or self.current_season
        dc = self.depth_charts.get(s, pd.DataFrame())
        if dc.empty:
            return None

        # Try sleeper_id first (Sleeper depth charts have 100% sleeper_id)
        if sleeper_id and "sleeper_id" in dc.columns:
            matches = dc[dc["sleeper_id"] == sleeper_id]
            if not matches.empty:
                return int(matches.iloc[0]["depth_rank"])

        # Fall back to gsis_id
        if gsis_id and "gsis_id" in dc.columns:
            matches = dc[dc["gsis_id"] == gsis_id]
            if not matches.empty:
                return int(matches.iloc[0]["depth_rank"])

        # Name fallback
        if name and "full_name" in dc.columns:
            matches = dc[dc["full_name"] == name]
            if not matches.empty:
                return int(matches.iloc[0]["depth_rank"])

        return None

    def get_team_depth_context(self, team: str, season: int | None = None) -> dict[str, list[dict]]:
        """
        Return full depth context for a team: position -> [{name, gsis_id, rank}].
        """
        s = season or self.current_season
        dc = self.depth_charts.get(s, pd.DataFrame())
        if dc.empty:
            return {}

        team_norm = _normalize_team_abbr(team)
        team_dc = dc[dc["team"].apply(_normalize_team_abbr) == team_norm].sort_values(
            ["position", "depth_rank"]
        )

        name_col = next(
            (c for c in ("full_name", "player_name") if c in dc.columns),
            None,
        )

        result: dict[str, list[dict]] = {}
        for _, row in team_dc.iterrows():
            pos = str(row["position"]).upper()
            entry = {
                "name": str(row[name_col]) if name_col else "",
                "gsis_id": str(row["gsis_id"]) if "gsis_id" in dc.columns and pd.notna(row.get("gsis_id")) else None,
                "rank": int(row["depth_rank"]),
            }
            result.setdefault(pos, []).append(entry)
        return result

    def summary(self) -> dict:
        return {
            "analysis_seasons": self.analysis_seasons,
            "current_season": self.current_season,
            "analysis_year": self.analysis_year,
            "data": {
                season: {
                    "seasonal_stats": len(self.seasonal_stats.get(season, [])),
                    "target_share": len(self.target_share.get(season, [])),
                    "qb_stats": len(self.qb_stats.get(season, [])),
                    "oline_stats": len(self.oline_stats.get(season, [])),
                    "injuries": len(self.injuries.get(season, [])),
                    "depth_charts": len(self.depth_charts.get(season, [])),
                }
                for season in self.analysis_seasons
            },
            "rosters": len(self.rosters),
            "schedule_year": self.schedule_year,
            "depth_charts_loaded": len(self.depth_charts),
        }


# ---------------------------------------------------------------------------
# gsis_id population from depth charts
# ---------------------------------------------------------------------------


async def populate_gsis_from_depth_charts(warehouse: "NflDataWarehouse") -> int:
    """
    Populate gsis_id on players table from depth chart data.

    Players with yahoo_player_id='nfl_XXX' should already have gsis_id set
    by the migration backfill or seed script. This function handles remaining
    players by matching name + team against depth chart data.

    Returns count of players updated.
    """
    from backend.database import AsyncSessionLocal
    from backend.models.player import Player
    from sqlalchemy import select

    dc = warehouse.get_depth_chart(warehouse.current_season)
    if dc.empty:
        logger.info("No depth chart data — skipping gsis_id population")
        return 0

    name_col = next(
        (c for c in ("full_name", "player_name") if c in dc.columns),
        None,
    )
    if not name_col or "gsis_id" not in dc.columns:
        return 0

    updated = 0
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Player).where(Player.gsis_id.is_(None))
        )
        players_without_gsis = result.scalars().all()

        for player in players_without_gsis:
            if not player.name or not player.team_abbr:
                continue

            norm_name = normalize_player_name(player.name)
            team_dc = dc[dc["team"].str.upper() == player.team_abbr.upper()]

            for _, row in team_dc.iterrows():
                dc_name = str(row.get(name_col, ""))
                dc_gsis = row.get("gsis_id")
                if not dc_gsis or pd.isna(dc_gsis):
                    continue
                if normalize_player_name(dc_name) == norm_name:
                    player.gsis_id = str(dc_gsis)
                    updated += 1
                    break

        await session.commit()

    logger.info("Populated gsis_id for %d players from depth charts", updated)
    return updated


# ---------------------------------------------------------------------------
# Draft capital and AV chart
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
