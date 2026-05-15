"""
Sleeper API integration — primary data source for player identity, rosters,
stats, depth charts, and injury status.

Replaces nfl_data_py for all player-level data.
No API key required. Rate limit: generous.
Data is updated daily by Sleeper.

Retained nfl_data_py usage (no Sleeper equivalent):
  - fetch_schedules()
  - import_pbp_data() / compute_team_oline_stats()
  - fetch_ngs_data() for CPOE/air yards
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

SLEEPER_BASE = "https://api.sleeper.app/v1"
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "data/cache")) / "sleeper"
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}
CACHE_TTL_HOURS = 24  # players/injuries refresh daily
CACHE_TTL_HISTORICAL = None  # historical stats: forever


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.parquet"


def _cache_valid(path: Path, ttl_hours: float | None) -> bool:
    """True if cache file exists and is within TTL."""
    if not path.exists():
        return False
    if ttl_hours is None:
        return True  # historical — never expires
    age = (time.time() - path.stat().st_mtime) / 3600
    return age < ttl_hours


# ---------------------------------------------------------------------------
# PLAYERS (rosters + depth charts + injuries)
# ---------------------------------------------------------------------------


def fetch_sleeper_players() -> pd.DataFrame:
    """
    Fetch all active NFL skill position players from Sleeper.
    Cache TTL: 24 hours (refreshes each pipeline run).

    Returns DataFrame with columns:
      player_id         — Sleeper's own ID (primary key)
      full_name         — reliable full name
      first_name, last_name
      position          — QB/RB/WR/TE
      team              — current team abbr (None = FA)
      status            — Active/Inactive/IR
      depth_chart_order — 1=starter (sparse but reliable)
      injury_status     — Questionable/IR/DNR/NA or None
      age               — current age
      years_exp         — NFL seasons of experience
      college           — college attended
      sportradar_id     — 100% coverage — PRIMARY cross-source ID
      gsis_id           — 29% coverage
      yahoo_id          — 46% coverage
      birth_date        — YYYY-MM-DD
      team_changed_at   — epoch ms of last team change
    """
    path = _cache_path("players_current")
    if _cache_valid(path, CACHE_TTL_HOURS):
        logger.info("Sleeper players: cache hit")
        return pd.read_parquet(path)

    logger.info("Fetching Sleeper players...")
    resp = requests.get(f"{SLEEPER_BASE}/players/nfl", timeout=30)
    resp.raise_for_status()

    df = pd.DataFrame(resp.json().values())
    if df.empty:
        logger.warning("Sleeper players: empty response")
        return df

    # Filter to active skill positions only
    # Include Inactive — injured starters matter
    skill = df[
        df["position"].isin(SKILL_POSITIONS)
        & df["status"].isin(["Active", "Inactive"])
    ].copy()

    # Normalize: empty string team → None (FA)
    skill["team"] = skill["team"].replace("", None)

    # Normalize team abbreviations to match pipeline conventions
    # Sleeper uses "LA" for Rams; our pipeline uses "LAR"
    _SLEEPER_TEAM_ALIASES = {"LA": "LAR", "OAK": "LV", "SD": "LAC", "WSH": "WAS"}
    skill["team"] = skill["team"].map(
        lambda t: _SLEEPER_TEAM_ALIASES.get(t, t) if pd.notna(t) else t
    )

    # depth_chart_order as nullable int
    if "depth_chart_order" in skill.columns:
        skill["depth_chart_order"] = pd.to_numeric(
            skill["depth_chart_order"], errors="coerce"
        )

    skill.to_parquet(path, index=False)
    logger.info("Sleeper players loaded: %d active skill players", len(skill))
    return skill


# ---------------------------------------------------------------------------
# SEASON STATS
# ---------------------------------------------------------------------------


def fetch_sleeper_season_stats(season: int) -> pd.DataFrame:
    """
    Fetch season stats from Sleeper.
    Historical seasons cached forever. Current season TTL: 24 hours.

    Raw fields from Sleeper include:
      pts_ppr, pts_half_ppr, pts_std, gp (games played),
      rec, rec_yd, rec_td, rec_tgt, rush_att, rush_yd, rush_td,
      pass_yd, pass_td, pass_int, pass_att, pass_cmp

    Returns DataFrame with internal column names
    (fantasy_points_ppr, games, receptions, etc.)
    plus sleeper_id for joining to player info.
    """
    from backend.utils.seasons import get_current_season

    is_historical = season < get_current_season()
    ttl = None if is_historical else CACHE_TTL_HOURS
    path = _cache_path(f"stats_{season}")

    if _cache_valid(path, ttl):
        logger.info("Sleeper stats %d: cache hit", season)
        return pd.read_parquet(path)

    logger.info("Fetching Sleeper season stats %d...", season)
    resp = requests.get(
        f"{SLEEPER_BASE}/stats/nfl/regular/{season}",
        timeout=30,
    )

    if resp.status_code == 404:
        logger.warning("Sleeper stats %d: not available", season)
        return pd.DataFrame()

    resp.raise_for_status()
    raw = resp.json()
    if not raw:
        return pd.DataFrame()

    rows = [{"sleeper_id": pid, **stats} for pid, stats in raw.items()]
    df = pd.DataFrame(rows)

    # Rename to internal schema
    df = df.rename(columns={
        "pts_ppr":      "fantasy_points_ppr",
        "pts_half_ppr": "fantasy_points_half_ppr",
        "pts_std":      "fantasy_points_std",
        "gp":           "games",
        "rec":          "receptions",
        "rec_yd":       "receiving_yards",
        "rec_td":       "receiving_tds",
        "rec_tgt":      "targets",
        "rush_att":     "rush_attempts",
        "rush_yd":      "rushing_yards",
        "rush_td":      "rushing_tds",
        "pass_yd":      "passing_yards",
        "pass_td":      "passing_tds",
        "pass_int":     "interceptions",
        "pass_att":     "attempts",
        "pass_cmp":     "completions",
    })
    df["season"] = season

    df.to_parquet(path, index=False)
    logger.info("Sleeper stats %d: %d players", season, len(df))
    return df


def get_sleeper_seasonal_stats(season: int) -> pd.DataFrame:
    """
    Season stats merged with player info.
    Drop-in replacement for nfl_data_py get_seasonal_stats().

    Returns DataFrame with:
      player_name, position, team, season,
      fantasy_points_ppr, games, receptions,
      receiving_yards, receiving_tds, targets,
      rush_attempts, rushing_yards, rushing_tds,
      passing_yards, passing_tds, interceptions,
      sleeper_id, sportradar_id, gsis_id
    """
    stats = fetch_sleeper_season_stats(season)
    if stats.empty:
        return pd.DataFrame()

    players = fetch_sleeper_players()
    if players.empty:
        return stats

    player_cols = [
        c for c in [
            "player_id", "full_name", "position",
            "team", "sportradar_id", "gsis_id",
            "depth_chart_order", "years_exp",
        ]
        if c in players.columns
    ]

    merged = stats.merge(
        players[player_cols].rename(columns={
            "player_id": "sleeper_id",
            "full_name": "player_name",
        }),
        on="sleeper_id",
        how="left",
    )

    # Keep only skill positions with stats
    if "position" in merged.columns:
        merged = merged[merged["position"].isin(SKILL_POSITIONS)]

    return merged


# ---------------------------------------------------------------------------
# DEPTH CHARTS
# ---------------------------------------------------------------------------


def get_sleeper_depth_charts() -> pd.DataFrame:
    """
    Current depth charts from Sleeper player data.
    depth_chart_order=1 is the starter.

    Returns DataFrame matching NflDataWarehouse depth_charts schema:
      team, player_name, pos_abb, pos_rank,
      sleeper_id, sportradar_id, gsis_id

    No roster cross-reference needed — Sleeper already has correct
    team assignments. (Rodgers correctly shown as FA/no team,
    Geno Smith correctly at NYJ depth=1)
    """
    players = fetch_sleeper_players()
    if players.empty:
        return pd.DataFrame()

    has_depth = players[players["depth_chart_order"].notna()].copy()

    if has_depth.empty:
        return pd.DataFrame()

    has_depth = has_depth.rename(columns={
        "full_name":         "player_name",
        "position":          "pos_abb",
        "depth_chart_order": "pos_rank",
        "player_id":         "sleeper_id",
    })
    has_depth["pos_rank"] = has_depth["pos_rank"].astype(int)

    keep = [
        c for c in [
            "team", "player_name", "pos_abb", "pos_rank",
            "sleeper_id", "sportradar_id", "gsis_id",
            "injury_status",
        ]
        if c in has_depth.columns
    ]

    return has_depth[keep].copy()


# ---------------------------------------------------------------------------
# INJURIES
# ---------------------------------------------------------------------------


def get_sleeper_injuries() -> pd.DataFrame:
    """
    Current injury status from Sleeper.
    Drop-in replacement for nfl_data_py fetch_injuries().

    Returns DataFrame with:
      player_name, position, team,
      injury_status (Questionable/IR/DNR/NA),
      sleeper_id, sportradar_id
    """
    players = fetch_sleeper_players()
    if players.empty:
        return pd.DataFrame()

    injured = players[players["injury_status"].notna()].copy()
    injured = injured.rename(columns={"full_name": "player_name"})

    keep = [
        c for c in [
            "player_name", "position", "team",
            "injury_status", "player_id",
            "sportradar_id", "gsis_id",
        ]
        if c in injured.columns
    ]

    return injured[keep]
