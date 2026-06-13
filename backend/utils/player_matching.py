"""
Shared player → season-stats resolution.

Extracted from PlayerProfilesAgent._get_player_season_stats so the
injury-risk agent's availability model can resolve the same way the
profiles agent does. ID-first matching (sleeper_id → sportradar_id →
gsis_id → name+position) is the only correct approach because the
season frames diverge by source: the 2025 frame uses abbreviated names
("C.McCaffrey") and gsis ids, while 2023/2024 use full names. A
name-only matcher silently drops the current season for every player.

Resolution reads the warehouse target_share frame (Sleeper-primary,
carries sleeper_id/sportradar_id/player_id and a games count).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from backend.integrations.nfl_data import NflDataWarehouse
    from backend.models.player import Player

logger = logging.getLogger(__name__)


def resolve_player_season_stats(
    player: "Player",
    season: int,
    warehouse: "NflDataWarehouse",
) -> dict | None:
    """Resolve one player's season stats from a Player ORM object.

    WR/RB/TE resolve from the target_share frame (sleeper_id-first,
    full names). QBs are not in target_share, so they resolve from the
    seasonal_stats frame by gsis_id + position guard. Returns the
    compact stats dict (including a ``games`` field) or None when no
    confident match exists.
    """
    if (player.position or "").upper() == "QB":
        return _resolve_qb_stats(player, season, warehouse)

    return resolve_player_season_stats_by_fields(
        warehouse,
        player_name=player.name,
        team=player.team_abbr or "",
        season=season,
        position=player.position or "",
        nfl_player_id=player.gsis_id,
        sleeper_id=player.sleeper_id,
        sportradar_id=player.sportradar_id,
    )


def _resolve_qb_stats(
    player: "Player",
    season: int,
    warehouse: "NflDataWarehouse",
) -> dict | None:
    """QB-specific resolver: gsis_id + position='QB' against seasonal_stats.

    The seasonal_stats frame carries no sleeper_id; its ``player_id``
    column holds the gsis id. The position guard prevents surname
    collisions (e.g. "Allen" → Josh Allen QB, never Braelon Allen RB;
    "Murray" → Kyler Murray QB, never an RB Murray).

    gsis is stripped on BOTH sides: a DB-level TRIM cleaned today's
    rows, but sync_rosters can reintroduce whitespace on the next run,
    so the resolver must not depend on DB hygiene.
    """
    stats = warehouse.get_seasonal_stats(season)
    if stats is None or stats.empty:
        return None

    gsis = (player.gsis_id or "").strip()
    if not gsis or "player_id" not in stats.columns:
        return None

    mask = stats["player_id"].astype(str).str.strip() == gsis
    if "position" in stats.columns:
        mask = mask & (stats["position"].astype(str).str.upper() == "QB")

    match = stats[mask]
    if match.empty:
        return None

    row = match.iloc[0]
    games = int(row.get("games", 0) or 0)
    if games == 0:
        return None

    ppr_total = row.get("fantasy_points_ppr")
    try:
        ppr_total = float(ppr_total) if ppr_total is not None and pd.notna(ppr_total) else None
    except (TypeError, ValueError):
        ppr_total = None

    return {
        "games": games,
        "ppr_per_game": round(ppr_total / games, 1) if ppr_total and games else None,
        "position": "QB",
    }


def resolve_player_season_stats_by_fields(
    warehouse: "NflDataWarehouse",
    player_name: str,
    team: str,
    season: int,
    position: str,
    *,
    nfl_player_id: str | None = None,
    sleeper_id: str | None = None,
    sportradar_id: str | None = None,
) -> dict | None:
    """Return compact season stats for one player from the target_share df.

    Position is REQUIRED to prevent cross-position name collisions
    (e.g. "B.Taylor" WR on IND must NOT match "J.Taylor" RB on IND).

    Match priority:
      0a. sleeper_id — best, 100% coverage from Sleeper
      0b. sportradar_id — 98% coverage
      1.  player_id column (gsis id) — no name ambiguity
      2.  last-name + team + SAME POSITION
      3.  last-name + first-initial cross-team + SAME POSITION fallback —
          ONLY when a known nfl_player_id verifies the candidate.
    """
    ts_df = warehouse.get_target_share(season)
    if ts_df is None:
        return None

    pos_upper = position.upper()
    has_position_col = "position" in ts_df.columns

    def _pos_filter(df: pd.DataFrame) -> pd.DataFrame:
        """Filter to same position. Critical to prevent cross-position collisions."""
        if has_position_col:
            return df[df["position"].str.upper() == pos_upper]
        return df

    def _extract(row: pd.Series) -> dict | None:
        games = int(row.get("games", 0) or 0)
        if games == 0:
            return None

        def _f(col: str, decimals: int = 3):
            v = row.get(col)
            try:
                return round(float(v), decimals) if v is not None and pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        targets = int(row.get("total_targets", 0) or 0)
        receptions = int(row.get("total_receptions", 0) or 0)
        return {
            "games":           games,
            "recent_team":     str(row.get("recent_team", "") or ""),
            "target_share":    _f("avg_target_share"),
            "air_yards_share": _f("avg_air_yards_share"),
            "targets":         targets,
            "receptions":      receptions,
            "rec_yards":       int(row.get("total_rec_yards",  0) or 0),
            "rec_tds":         int(row.get("total_rec_tds",    0) or 0),
            "carries":         int(row.get("total_carries",    0) or 0),
            "rush_yards":      int(row.get("total_rush_yards", 0) or 0),
            "rush_tds":        int(row.get("total_rush_tds",   0) or 0),
            "ppr_per_game":    _f("ppr_per_game", 1),
            # Efficiency fields
            "rush_ypa":        _f("rush_ypa", 2),
            "rush_btkl":       _f("rush_btkl", 0),
            "rec_ypr":         _f("rec_ypr", 2),
            "snap_count":      _f("off_snp", 0),
            "rush_fd":         _f("rush_fd", 0),
            "rec_fd":          _f("rec_fd", 0),
            "catch_pct": (
                round(receptions / targets * 100, 1)
                if targets > 0 else None
            ),
        }

    def _extract_combined(rows: pd.DataFrame) -> dict | None:
        """Aggregate stats across multi-team splits for the same player."""
        def _int_sum(col: str) -> int:
            return int(rows[col].fillna(0).sum()) if col in rows.columns else 0

        total_games = _int_sum("games")
        if total_games == 0:
            return None

        # Use the team with the most games as the primary team
        primary_team = rows.loc[rows["games"].fillna(0).astype(int).idxmax(), "recent_team"]

        # Games-weighted average for rate stats
        game_weights = rows["games"].fillna(0).astype(float)
        weight_sum = game_weights.sum()

        def _weighted_avg(col: str, decimals: int = 3):
            if col not in rows.columns:
                return None
            vals = rows[col].apply(
                lambda v: float(v) if v is not None and pd.notna(v) else 0.0
            )
            avg = (vals * game_weights).sum() / weight_sum if weight_sum > 0 else 0.0
            return round(avg, decimals) if avg else None

        receptions = _int_sum("total_receptions")
        rec_yards = _int_sum("total_rec_yards")
        rec_tds = _int_sum("total_rec_tds")
        rush_yards = _int_sum("total_rush_yards")
        rush_tds = _int_sum("total_rush_tds")

        # PPR cross-validation against source fantasy_points_ppr
        computed_ppr = receptions * 1.0 + (rec_yards + rush_yards) * 0.1 + (rec_tds + rush_tds) * 6.0
        fantasy_ppr = float(rows["total_fantasy_points"].fillna(0).sum()) if "total_fantasy_points" in rows.columns else 0.0
        if fantasy_ppr > 0 and abs(computed_ppr - fantasy_ppr) / fantasy_ppr > 0.15:
            pid = rows.iloc[0].get("player_id", "?")
            logger.warning(
                "PPR divergence for player_id=%s: computed=%.1f vs source=%.1f",
                pid, computed_ppr, fantasy_ppr,
            )

        total_targets = _int_sum("total_targets")
        return {
            "games":           total_games,
            "recent_team":     str(primary_team or ""),
            "target_share":    _weighted_avg("avg_target_share"),
            "air_yards_share": _weighted_avg("avg_air_yards_share"),
            "targets":         total_targets,
            "receptions":      receptions,
            "rec_yards":       rec_yards,
            "rec_tds":         rec_tds,
            "carries":         _int_sum("total_carries"),
            "rush_yards":      rush_yards,
            "rush_tds":        rush_tds,
            "ppr_per_game":    _weighted_avg("ppr_per_game", 1),
            # Efficiency fields
            "rush_ypa":        _weighted_avg("rush_ypa", 2),
            "rush_btkl":       _int_sum("rush_btkl") or None,
            "rec_ypr":         _weighted_avg("rec_ypr", 2),
            "snap_count":      _int_sum("off_snp") or None,
            "rush_fd":         _int_sum("rush_fd") or None,
            "rec_fd":          _int_sum("rec_fd") or None,
            "catch_pct": (
                round(receptions / total_targets * 100, 1)
                if total_targets > 0 else None
            ),
        }

    # --- Path 0a: sleeper_id match (best — 100% coverage from Sleeper) ---
    if sleeper_id and "sleeper_id" in ts_df.columns:
        id_rows = ts_df[ts_df["sleeper_id"] == sleeper_id]
        if not id_rows.empty:
            if len(id_rows) == 1:
                return _extract(id_rows.iloc[0])
            return _extract_combined(id_rows)

    # --- Path 0b: sportradar_id match (98% coverage) ---
    if sportradar_id and "sportradar_id" in ts_df.columns:
        id_rows = ts_df[ts_df["sportradar_id"] == sportradar_id]
        if not id_rows.empty:
            if len(id_rows) == 1:
                return _extract(id_rows.iloc[0])
            return _extract_combined(id_rows)

    # --- Path 1: player_id match (gsis_id — 29% coverage) ---
    if nfl_player_id and "player_id" in ts_df.columns:
        id_rows = ts_df[ts_df["player_id"] == nfl_player_id]
        if not id_rows.empty:
            if len(id_rows) == 1:
                return _extract(id_rows.iloc[0])
            # Multiple rows = multi-team season — aggregate across splits
            return _extract_combined(id_rows)

    # --- Path 2: last-name + team + SAME POSITION ---
    last = player_name.split()[-1]
    mask = (
        ts_df["player_name"].str.contains(last, case=False, na=False) &
        (ts_df["recent_team"] == team)
    )
    rows = _pos_filter(ts_df[mask]).sort_values("games", ascending=False)

    # Always disambiguate by first initial — even with one match,
    # a different initial means a different player (e.g. Isaiah Jacobs
    # vs Josh Jacobs on GB).  Handles both abbreviated ("D.Samuel")
    # and full name ("Deebo Samuel") formats.
    if not rows.empty:
        first_initial = player_name.split()[0][0].upper()
        initial_rows = rows[
            rows["player_name"].str[0].str.upper() == first_initial
        ]
        if not initial_rows.empty:
            rows = initial_rows
        elif nfl_player_id and "player_id" in rows.columns:
            # Initial mismatch — verify by ID before attributing
            id_rows = rows[rows["player_id"] == nfl_player_id]
            if not id_rows.empty:
                rows = id_rows
            else:
                rows = rows.iloc[0:0]  # ID mismatch — wrong player
        elif len(rows) == 1:
            # Single match, wrong initial, no ID to verify — refuse
            rows = rows.iloc[0:0]

    if not rows.empty:
        return _extract(rows.iloc[0])

    # --- Path 3: cross-team fallback (pre-trade history) + SAME POSITION ---
    # Only use when the caller has a known nfl_player_id that matches
    # the candidate's player_id. Without an ID to verify, cross-team
    # fallback risks attributing stats from a different player who
    # shares the same initial+last name (e.g. J'Mari Taylor ≠ Jonathan Taylor).
    if not nfl_player_id:
        return None  # No ID to verify — refuse cross-team attribution

    first_initial = player_name.split()[0][0].upper()
    all_last = _pos_filter(
        ts_df[ts_df["player_name"].str.contains(last, case=False, na=False)]
    )
    initial_fallback = all_last[all_last["player_name"].str.startswith(f"{first_initial}.")]
    candidates = initial_fallback if not initial_fallback.empty else all_last

    if "player_id" in candidates.columns:
        # Verify the candidate's player_id matches the caller's ID
        id_match = candidates[candidates["player_id"] == nfl_player_id]
        if not id_match.empty:
            return _extract(id_match.sort_values("games", ascending=False).iloc[0])
        # ID mismatch — different player with same name
        return None
    elif len(candidates["player_name"].unique()) != 1:
        return None  # No player_id column but multiple name variants

    if candidates.empty:
        return None
    return _extract(candidates.sort_values("games", ascending=False).iloc[0])
