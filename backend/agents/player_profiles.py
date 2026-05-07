"""
Agent 3: Player Profiles Agent

Builds a complete individual profile for every draftable skill-position player.
Inherits team system context from Agent 1 and dependency flags from Agent 2.

Architecture:
  - Model: Haiku (data extraction and classification)
  - Max tokens: 1000 per team batch
  - Pattern: pre-aggregate in Python → ONE call_once() per team → parse JSON array → write DB
  - Never uses run_agent() (that is for live draft only)

Key outputs per player:
  - Role classification (wr1_alpha, workhorse, etc.)
  - Clean season baseline (strips injury-shortened and backup-QB seasons)
  - Breakout candidate detection
  - Efficiency signal, age curve, situation score
"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import ClassVar

import pandas as pd
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU
from backend.agents.team_systems import NFL_TEAMS
from backend.database import AsyncSessionLocal
from backend.integrations import nfl_data
from backend.integrations.nfl_data import normalize_player_name, build_player_lookup
from backend.models.player import Player, PlayerProfile
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

logger = logging.getLogger(__name__)

SKILL_POSITIONS = {"QB", "WR", "RB", "TE"}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fantasy football player analyst building a pre-draft research database.

You receive pre-aggregated multi-season stats for every skill-position player on one NFL team.
Produce a JSON array — one profile object per player.

Each object must match this schema exactly:
{
  "player_name": "string",
  "role_classification": "string",
  "separation_score": "string (elite/above_avg/avg/below_avg)",
  "yards_after_catch_score": "string (elite/above_avg/avg/below_avg)",
  "efficiency_signal": "string (elite/above_avg/avg/below_avg)",
  "age_curve_position": "string (ascending/peak/descending)",
  "career_trajectory": "string (breakout/rising/established/declining/volatile)",
  "clean_season_baseline": {"receptions": int, "yards": int, "touchdowns": int, "ppr_points": float},
  "anomalous_seasons_excluded": [int],
  "breakout_flag": boolean,
  "breakout_reasoning": "string or null",
  "positional_scarcity_tier": "string (scarce/moderate/deep)",
  "situation_score": "string (strong/moderate/weak/volatile)"
}

role_classification MUST match the player's actual position — never cross-assign roles:
  QB players → use only: qb_elite, qb_starter, qb_streamer, qb_backup
  WR players → use only: wr1_alpha, slot_specialist, deep_threat, possession_wr2, gadget
  RB players → use only: workhorse, early_down_thumper, pass_catching_specialist, committee_back
  TE players → use only: te1_inline, te1_pass_catcher, te2_blocker, te2_flex

Rules:
- anomalous_seasons_excluded: include year integers where data shows games < 10 OR backup_qb_season=true
- clean_season_baseline: project stats from non-excluded seasons. If no clean seasons exist, use all available.
  "ppr_points" is the projected season TOTAL (not per-game). Example: a WR2 over 17 games ≈ 200.0.
- breakout_flag = true if ANY of: Year 2 or 3 player, path opened by departure in dependency_flags,
  new scheme elevates this player type, efficiency metrics already exceed production statistics.
- situation_score: strong for high system grade + elite QB + no displacement;
  volatile or weak for displaced/committee flags or rookie QB; moderate otherwise.
- Age curve peaks: QB 26-32, RB 24-26, WR 24-29, TE 26-29. ascending = before peak; descending = past peak.
- Contract year flag (contract_year=true) → slight upward bias in trajectory.
- compound_risk_flag on team → all players lean toward volatile/weak situation_score.

Additional NGS efficiency data in player seasons (when present):
  avg_separation: average yards of separation at catch point; higher = elite route running.
  avg_yac_above_expectation: yards after catch above model expectation; positive = elite YAC.
  rush_yards_over_expected_per_att: rushing yards over expected; positive = above-avg vision/burst.
Use these numeric signals to inform separation_score, yards_after_catch_score, and efficiency_signal.

For players where all seasons show games=0 (rookies or new acquisitions with no NFL history):
  - Still include them; classify by position, team system, and dependency_flags.
  - Set career_trajectory="volatile" unless a clear signal exists.
  - Set clean_season_baseline to position-typical conservative estimates.
  - Set breakout_flag=true if any dependency_flag has type "beneficiary".

Output ONLY a valid JSON array. No explanation, no preamble, no markdown fences.
Your entire response must be parseable by json.loads()."""


# ---------------------------------------------------------------------------
# PlayerProfilesAgent
# ---------------------------------------------------------------------------

class PlayerProfilesAgent(BaseAgent):
    AGENT_NAME       = "player_profiles"
    AGENT_MODEL      = HAIKU
    AGENT_MAX_TOKENS = 4000

    # Pattern 3: pre-warm once in run_all_teams(), reuse per team
    _data_cache: ClassVar[dict] = {}

    # ------------------------------------------------------------------
    # Sync data helpers — read from _data_cache (no network calls)
    # ------------------------------------------------------------------

    def _get_team_roster(self, team: str, season: int) -> list[dict]:
        """Return skill-position players on this team from cached roster data."""
        rosters = self._data_cache.get(f"rosters_{season}")
        if rosters is None:
            return []

        team_col = next((c for c in ("team", "team_abbr") if c in rosters.columns), None)
        name_col = next((c for c in ("full_name", "player_name") if c in rosters.columns), None)
        if not team_col or not name_col or "position" not in rosters.columns:
            return []

        mask = (
            (rosters[team_col].str.upper() == team.upper()) &
            rosters["position"].isin(SKILL_POSITIONS)
        )
        team_df = rosters[mask].copy()

        # Deduplicate: latest week per player
        if "week" in team_df.columns:
            team_df = (
                team_df.sort_values("week", ascending=False)
                .drop_duplicates(subset=[name_col])
            )

        result = []
        for _, row in team_df.iterrows():
            name = str(row.get(name_col, "")).strip()
            pos  = str(row.get("position", "")).strip().upper()
            if not name or pos not in SKILL_POSITIONS:
                continue
            entry: dict = {"name": name, "position": pos}
            age_val = row.get("age")
            if age_val is not None and pd.notna(age_val):
                entry["age"] = int(age_val)
            entry["contract_year"] = bool(row.get("contract_year", False))
            # Include nfl gsis player_id for reliable cross-source matching
            pid = row.get("player_id")
            if pid and pd.notna(pid):
                entry["nfl_player_id"] = str(pid).strip()
            result.append(entry)

        return result

    def _is_backup_qb_season(self, team: str, season: int) -> bool:
        """True if the team's backup QB started 4+ regular-season games in this season."""
        weekly = self._data_cache.get(f"weekly_{season}")
        if weekly is None:
            return False
        # Use REG season only — postseason/preseason weeks inflate QB game counts
        w = weekly
        if "season_type" in w.columns:
            w = w[w["season_type"] == "REG"]
        qbs = w[
            (w["recent_team"] == team) & (w["position"] == "QB")
        ]
        if qbs.empty:
            return False
        qb_games = (
            qbs.groupby("player_name")["week"]
            .count()
            .sort_values(ascending=False)
        )
        return len(qb_games) >= 2 and int(qb_games.iloc[1]) >= 4

    def _get_player_season_stats(
        self, player_name: str, team: str, season: int,
        nfl_player_id: str | None = None,
    ) -> dict | None:
        """Return compact season stats for one player from the cached target_share df.

        Match priority:
          1. player_id column (gsis id) — 100% reliable, no name ambiguity
          2. last-name + team filter — handles most veterans reliably
          3. last-name + first-initial cross-team fallback — ONLY when exactly ONE
             unique player_id has that initial+last combo to avoid wrong-player stats
        """
        ts_df = self._data_cache.get(f"target_share_{season}")
        if ts_df is None:
            return None

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
            return {
                "games":           games,
                "recent_team":     str(row.get("recent_team", "") or ""),
                "target_share":    _f("avg_target_share"),
                "air_yards_share": _f("avg_air_yards_share"),
                "targets":         int(row.get("total_targets",    0) or 0),
                "receptions":      int(row.get("total_receptions", 0) or 0),
                "rec_yards":       int(row.get("total_rec_yards",  0) or 0),
                "rec_tds":         int(row.get("total_rec_tds",    0) or 0),
                "carries":         int(row.get("total_carries",    0) or 0),
                "rush_yards":      int(row.get("total_rush_yards", 0) or 0),
                "rush_tds":        int(row.get("total_rush_tds",   0) or 0),
                "ppr_per_game":    _f("ppr_per_game", 1),
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

            return {
                "games":           total_games,
                "recent_team":     str(primary_team or ""),
                "target_share":    _weighted_avg("avg_target_share"),
                "air_yards_share": _weighted_avg("avg_air_yards_share"),
                "targets":         _int_sum("total_targets"),
                "receptions":      receptions,
                "rec_yards":       rec_yards,
                "rec_tds":         rec_tds,
                "carries":         _int_sum("total_carries"),
                "rush_yards":      rush_yards,
                "rush_tds":        rush_tds,
                "ppr_per_game":    _weighted_avg("ppr_per_game", 1),
            }

        # --- Path 1: player_id match (most reliable) ---
        if nfl_player_id and "player_id" in ts_df.columns:
            id_rows = ts_df[ts_df["player_id"] == nfl_player_id]
            if not id_rows.empty:
                if len(id_rows) == 1:
                    return _extract(id_rows.iloc[0])
                # Multiple rows = multi-team season — aggregate across splits
                return _extract_combined(id_rows)

        # --- Path 2: last-name + team filter ---
        last = player_name.split()[-1]
        mask = (
            ts_df["player_name"].str.contains(last, case=False, na=False) &
            (ts_df["recent_team"] == team)
        )
        rows = ts_df[mask].sort_values("games", ascending=False)

        # Disambiguate same-last-name same-team players by first initial
        if len(rows) > 1:
            first_initial = player_name.split()[0][0].upper()
            initial_rows = rows[rows["player_name"].str.startswith(f"{first_initial}.")]
            if not initial_rows.empty:
                rows = initial_rows

        if not rows.empty:
            return _extract(rows.iloc[0])

        # --- Path 3: cross-team fallback (pre-trade history) ---
        # Only use when there is exactly ONE unique player_id with this initial+last
        # across all teams to prevent wrong-player attribution (e.g. "JaQuae Jackson"
        # getting another team's J.Jackson stats).
        first_initial = player_name.split()[0][0].upper()
        all_last = ts_df[ts_df["player_name"].str.contains(last, case=False, na=False)]
        initial_fallback = all_last[all_last["player_name"].str.startswith(f"{first_initial}.")]
        candidates = initial_fallback if not initial_fallback.empty else all_last

        if "player_id" in candidates.columns:
            unique_ids = candidates["player_id"].nunique()
            if unique_ids != 1:
                return None  # Ambiguous — refuse to attribute wrong player's stats
        elif len(candidates["player_name"].unique()) != 1:
            return None  # No player_id column but multiple name variants

        if candidates.empty:
            return None
        return _extract(candidates.sort_values("games", ascending=False).iloc[0])

    def _get_qb_season(
        self, player_name: str, team: str, season: int,
        nfl_player_id: str | None = None,
    ) -> dict | None:
        """Return QB-specific season stats from cached qb_season data."""
        qb_df = self._data_cache.get(f"qb_season_{season}")
        if qb_df is None:
            return None

        # Match by player_id first (most reliable)
        match = pd.DataFrame()
        if nfl_player_id and "player_id" in qb_df.columns:
            match = qb_df[qb_df["player_id"] == nfl_player_id]

        # Fallback: name + team
        if match.empty:
            normalized = normalize_player_name(player_name)
            if "player_name" in qb_df.columns:
                qb_df_team = qb_df[qb_df["recent_team"] == team]
                for _, row in qb_df_team.iterrows():
                    if normalize_player_name(str(row.get("player_name", ""))) == normalized:
                        match = qb_df_team[qb_df_team.index == row.name]
                        break

        if match.empty:
            return None

        row = match.iloc[0]
        games = int(row.get("games", 0) or 0)
        if games == 0:
            return None

        def _safe_float(col: str, decimals: int = 1):
            v = row.get(col)
            try:
                return round(float(v), decimals) if v is not None and pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        return {
            "games":              games,
            "recent_team":        str(row.get("recent_team", team)),
            "completions":        int(row.get("completions", 0) or 0),
            "attempts":           int(row.get("attempts", 0) or 0),
            "completion_pct":     _safe_float("completion_pct", 3),
            "passing_yards":      int(row.get("passing_yards", 0) or 0),
            "passing_tds":        int(row.get("passing_tds", 0) or 0),
            "interceptions":      int(row.get("interceptions", 0) or 0),
            "sacks":              int(row.get("sacks", 0) or 0),
            "cpoe":               _safe_float("cpoe", 2),
            "avg_time_to_throw":  _safe_float("avg_time_to_throw", 3),
            "rushing_yards":      int(row.get("rushing_yards", 0) or 0),
            "rushing_tds":        int(row.get("rushing_tds", 0) or 0),
            "carries":            int(row.get("carries", 0) or 0),
            "fantasy_points_ppr": _safe_float("fantasy_points_ppr", 1),
            "ppr_per_game":       _safe_float("ppr_per_game", 1),
        }

    def _get_snap_pct(self, player_name: str, team: str, season: int) -> float | None:
        """Return avg offensive snap % from the cached snap_pct df."""
        snap_df = self._data_cache.get(f"snap_pct_{season}")
        if snap_df is None:
            return None

        name_col = next((c for c in ("player", "player_name") if c in snap_df.columns), None)
        team_col = next((c for c in ("team", "team_abbr") if c in snap_df.columns), None)
        if not name_col or not team_col:
            return None

        last = player_name.split()[-1]
        mask = (
            snap_df[name_col].str.contains(last, case=False, na=False) &
            (snap_df[team_col].str.upper() == team.upper())
        )
        rows = snap_df[mask]
        if rows.empty:
            return None

        v = rows.iloc[0].get("avg_snap_pct")
        try:
            return round(float(v), 3) if v is not None and pd.notna(v) else None
        except (TypeError, ValueError):
            return None

    def _get_ngs_receiving_stats(self, player_name: str, team: str, season: int) -> dict:
        """Return NGS receiving metrics (separation, YAC) from cached aggregated data."""
        ngs = self._data_cache.get(f"ngs_receiving_{season}")
        if ngs is None or ngs.empty:
            return {}

        last = player_name.split()[-1]
        mask = ngs["player_display_name"].str.contains(last, case=False, na=False)
        if "team_abbr" in ngs.columns:
            mask = mask & (ngs["team_abbr"].str.upper() == team.upper())
        rows = ngs[mask]
        if rows.empty:
            return {}

        row = rows.iloc[0]
        result = {}
        for col in ("avg_separation", "avg_yac_above_expectation"):
            v = row.get(col)
            try:
                if v is not None and pd.notna(v):
                    result[col] = round(float(v), 2)
            except (TypeError, ValueError):
                pass
        return result

    def _get_ngs_rushing_stats(self, player_name: str, team: str, season: int) -> dict:
        """Return NGS rushing metrics (yards over expected) from cached aggregated data."""
        ngs = self._data_cache.get(f"ngs_rushing_{season}")
        if ngs is None or ngs.empty:
            return {}

        last = player_name.split()[-1]
        mask = ngs["player_display_name"].str.contains(last, case=False, na=False)
        if "team_abbr" in ngs.columns:
            mask = mask & (ngs["team_abbr"].str.upper() == team.upper())
        rows = ngs[mask]
        if rows.empty:
            return {}

        row = rows.iloc[0]
        result = {}
        for col in ("rush_yards_over_expected_per_att", "rush_pct_over_expected"):
            v = row.get(col)
            try:
                if v is not None and pd.notna(v):
                    result[col] = round(float(v), 2)
            except (TypeError, ValueError):
                pass
        return result

    # ------------------------------------------------------------------
    # Async DB helpers
    # ------------------------------------------------------------------

    async def _get_team_system(self, team: str) -> dict:
        from backend.models.team_system import TeamSystem
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(TeamSystem).where(TeamSystem.team_abbr == team)
            )
            ts = result.scalar_one_or_none()
            if not ts:
                return {}
            return {
                "system_grade":       ts.system_grade,
                "qb_name":            ts.qb_name,
                "qb_tier":            ts.qb_tier,
                "rookie_qb_flag":     ts.rookie_qb_flag,
                "compound_risk_flag": ts.compound_risk_flag,
                "oc_scheme":          ts.oc_scheme,
                "red_zone_philosophy": ts.red_zone_philosophy,
            }

    async def _get_team_rookie_fields(self, team: str) -> dict[str, dict]:
        """
        Fetch rookie evaluation fields (written by Agent 2) for all rookies on this team.
        Returns {player_name: {is_rookie, comp_yr1_avg_ppg, ...}}.
        One DB query per team. Returns empty dict on any DB error.
        """
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Player).where(
                        Player.team_abbr == team,
                        Player.is_rookie.is_(True),
                    )
                )
                players = result.scalars().all()
        except Exception as exc:
            logger.debug("Could not fetch rookie fields for %s: %s", team, exc)
            return {}

        fields: dict[str, dict] = {}
        for p in players:
            fields[p.name] = {
                "is_rookie":            True,
                "college_profile_grade": p.college_profile_grade,
                "draft_capital_signal":  p.draft_capital_signal,
                "landing_spot_modifier": float(p.landing_spot_modifier) if p.landing_spot_modifier else 1.0,
                "comp_yr1_avg_ppg":      float(p.comp_yr1_avg_ppg) if p.comp_yr1_avg_ppg else None,
                "comp_yr2_avg_ppg":      float(p.comp_yr2_avg_ppg) if p.comp_yr2_avg_ppg else None,
                "historical_comp_names": p.historical_comp_names or [],
                "depth_chart_rank":      2,  # default; Agent 2 sets via displacement flags
            }
        return fields

    async def _get_team_dependency_flags(self, team: str) -> dict[str, list[dict]]:
        """Return {player_name: [compact_flag_dicts]} for all players on this team."""
        from backend.models.dependency import PlayerDependency

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PlayerDependency, Player.name)
                .join(Player, PlayerDependency.player_id == Player.id)
                .where(Player.team_abbr == team)
            )
            rows = result.all()

        flags_by_player: dict[str, list[dict]] = {}
        for dep, player_name in rows:
            flags_by_player.setdefault(player_name, []).append({
                "type":       dep.flag_type,
                "trigger":    dep.trigger_player_name,
                "effect":     dep.effect_on_value,
                "confidence": dep.confidence,
            })
        return flags_by_player

    # ------------------------------------------------------------------
    # NGS cache aggregation — weekly NGS → season-level per player
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_ngs(raw: pd.DataFrame, avg_cols: list[str]) -> pd.DataFrame:
        """Aggregate weekly NGS data to season-level means per player+team."""
        group_cols = [c for c in ("player_display_name", "team_abbr") if c in raw.columns]
        if not group_cols or raw.empty:
            return pd.DataFrame()
        valid_cols = [c for c in avg_cols if c in raw.columns]
        if not valid_cols:
            return pd.DataFrame()
        return raw.groupby(group_cols)[valid_cols].mean().reset_index()

    # ------------------------------------------------------------------
    # On-demand cache loader — used by single-team runs
    # ------------------------------------------------------------------

    def _ensure_cache_loaded(self, analysis_seasons: list[int], current_season: int) -> None:
        """Load data caches if not already populated (single-team runs bypass run_all_teams)."""
        for season in analysis_seasons:
            if f"target_share_{season}" not in self._data_cache:
                try:
                    self._data_cache[f"target_share_{season}"] = nfl_data.compute_target_share(season)
                    logger.info("Loaded target_share %d on demand", season)
                except Exception as exc:
                    logger.warning("Could not load target_share %d: %s", season, exc)

            if f"weekly_{season}" not in self._data_cache:
                try:
                    self._data_cache[f"weekly_{season}"] = nfl_data.fetch_weekly_stats(season)
                    logger.info("Loaded weekly_stats %d on demand", season)
                except Exception as exc:
                    logger.warning("Could not load weekly_stats %d: %s", season, exc)

            if f"ngs_receiving_{season}" not in self._data_cache:
                try:
                    raw = nfl_data.fetch_ngs_data("receiving", season)
                    self._data_cache[f"ngs_receiving_{season}"] = self._aggregate_ngs(
                        raw, ["avg_separation", "avg_yac_above_expectation"]
                    )
                    logger.info("Loaded ngs_receiving %d on demand", season)
                except Exception as exc:
                    logger.warning("Could not load ngs_receiving %d: %s", season, exc)

            if f"ngs_rushing_{season}" not in self._data_cache:
                try:
                    raw = nfl_data.fetch_ngs_data("rushing", season)
                    self._data_cache[f"ngs_rushing_{season}"] = self._aggregate_ngs(
                        raw, ["rush_yards_over_expected_per_att", "rush_pct_over_expected"]
                    )
                    logger.info("Loaded ngs_rushing %d on demand", season)
                except Exception as exc:
                    logger.warning("Could not load ngs_rushing %d: %s", season, exc)

            if f"qb_season_{season}" not in self._data_cache:
                try:
                    self._data_cache[f"qb_season_{season}"] = nfl_data.compute_qb_season_stats(season)
                    logger.info("Loaded qb_season %d on demand", season)
                except Exception as exc:
                    logger.warning("Could not load qb_season %d: %s", season, exc)

        if f"rosters_{current_season}" not in self._data_cache:
            try:
                self._data_cache[f"rosters_{current_season}"] = nfl_data.fetch_rosters(current_season)
                logger.info("Loaded rosters %d on demand", current_season)
            except Exception as exc:
                logger.warning("Could not load rosters %d: %s", current_season, exc)

        if f"snap_pct_{current_season}" not in self._data_cache:
            try:
                self._data_cache[f"snap_pct_{current_season}"] = nfl_data.compute_snap_pct(current_season)
                logger.info("Loaded snap_pct %d on demand", current_season)
            except Exception as exc:
                logger.warning("Could not load snap_pct %d: %s", current_season, exc)

    # ------------------------------------------------------------------
    # Context builder — all Python, zero API calls
    # ------------------------------------------------------------------

    async def _build_team_context(self, team_abbr: str) -> dict:
        team             = team_abbr.upper()
        analysis_seasons = get_analysis_seasons(3)
        current_season   = get_current_season()
        analysis_year    = get_analysis_year()

        self._ensure_cache_loaded(analysis_seasons, current_season)

        team_system   = await self._get_team_system(team)
        dep_flags     = await self._get_team_dependency_flags(team)
        rookie_fields = await self._get_team_rookie_fields(team)

        backup_qb_flags = {
            s: self._is_backup_qb_season(team, s) for s in analysis_seasons
        }

        roster = self._get_team_roster(team, current_season)
        seen:    set[str]   = set()
        players: list[dict] = []

        for info in roster:
            pname = info["name"]
            if pname in seen:
                continue
            seen.add(pname)

            nfl_pid = info.get("nfl_player_id")
            pos = info["position"]
            seasons_data: list[dict] = []

            if pos == "QB":
                # QB branch: use QB-specific passing stats
                for season in analysis_seasons:
                    stats = self._get_qb_season(pname, team, season, nfl_player_id=nfl_pid)
                    if stats:
                        stats["year"] = season
                        seasons_data.append(stats)
                    else:
                        seasons_data.append({"year": season, "games": 0, "note": "no data"})
            else:
                # WR/RB/TE branch: use target_share data
                for season in analysis_seasons:
                    stats = self._get_player_season_stats(pname, team, season, nfl_player_id=nfl_pid)
                    if stats:
                        stats["year"]             = season
                        # Only apply backup_qb flag to WRs/TEs whose production is
                        # depressed by backup QB play.  RBs are largely unaffected.
                        stat_team = stats.get("recent_team", team)
                        stats["backup_qb_season"] = (
                            backup_qb_flags.get(season, False)
                            if pos in ("WR", "TE") and stat_team.upper() == team.upper()
                            else False
                        )
                        # Attach NGS efficiency data per position
                        if pos in ("WR", "TE"):
                            ngs = self._get_ngs_receiving_stats(pname, team, season)
                            if ngs:
                                stats.update(ngs)
                        elif pos == "RB":
                            ngs = self._get_ngs_rushing_stats(pname, team, season)
                            if ngs:
                                stats.update(ngs)
                        seasons_data.append(stats)
                    else:
                        seasons_data.append({
                            "year":             season,
                            "games":            0,
                            "backup_qb_season": backup_qb_flags.get(season, False),
                            "note":             "no data",
                        })

            # Skip only players with zero history AND no dependency flags
            # (rookies with dependency flags still get profiled; pure depth with nothing to say are skipped)
            has_any_data    = any(s.get("games", 0) > 0 for s in seasons_data)
            has_flags       = bool(dep_flags.get(pname, []))
            if not has_any_data and not has_flags:
                continue

            player_entry: dict = {
                "name":             pname,
                "position":         info["position"],
                "age":              info.get("age"),
                "contract_year":    info.get("contract_year", False),
                "snap_pct":         self._get_snap_pct(pname, team, current_season),
                "seasons":          seasons_data,
                "dependency_flags": dep_flags.get(pname, []),
                "nfl_player_id":    nfl_pid,  # pass through for DB ID resolution
            }
            # Merge rookie evaluation fields from Agent 2 (if applicable)
            if pname in rookie_fields:
                player_entry.update(rookie_fields[pname])
            players.append(player_entry)

        # Sort by most recent season PPR descending so token-limited output
        # profiles the highest-value players first.
        def _sort_key(p: dict) -> float:
            seasons = sorted(p.get("seasons", []), key=lambda s: s.get("year", 0), reverse=True)
            for s in seasons:
                v = s.get("ppr_per_game")
                if v and v > 0:
                    return float(v)
            return 0.0

        players.sort(key=_sort_key, reverse=True)

        return {
            "team":          team,
            "analysis_year": analysis_year,
            "team_system":   team_system,
            "players":       players,
        }

    # ------------------------------------------------------------------
    # Per-team runner — exactly ONE call_once()
    # ------------------------------------------------------------------

    async def run_for_team(self, team_abbr: str) -> int:
        """Run for one team. Returns number of profile records written."""
        team = team_abbr.upper()
        logger.info("Building player profiles context for %s", team)

        try:
            context = await self._build_team_context(team)

            if not context["players"]:
                logger.info("%s: no skill-position players with data, skipping", team)
                return 0

            raw = await self.call_once(
                system=SYSTEM_PROMPT,
                user=(
                    f"Build player profiles for the {team} skill-position players "
                    f"using this pre-aggregated data:\n\n"
                    f"{json.dumps(context, default=str)}"
                ),
                input_data=context,
                entity_id=team,
            )

            if not raw:
                return 0  # dry_run

            profiles = parse_json_output(raw)
            if isinstance(profiles, dict):
                profiles = [profiles]
            if not isinstance(profiles, list):
                logger.error("%s: unexpected output type: %s", team, type(profiles))
                return 0

            written = await _write_profiles(profiles, context, team)
            logger.info("%s: %d profiles written", team, written)
            return written

        except Exception as exc:
            logger.error("Player Profiles Agent failed for %s: %s", team, exc, exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Full pipeline — pre-warm caches once, then run all 32 teams
    # ------------------------------------------------------------------

    async def run_all_teams(self, concurrency: int = 4) -> dict[str, int]:
        """
        Pre-loads all shared data caches ONCE before running teams concurrently.
        Returns {team_abbr: profiles_written}.
        """
        analysis_seasons = get_analysis_seasons(3)
        current_season   = get_current_season()

        logger.info("Pre-loading Player Profiles data for seasons %s...", analysis_seasons)
        for season in analysis_seasons:
            if f"target_share_{season}" not in self._data_cache:
                try:
                    self._data_cache[f"target_share_{season}"] = nfl_data.compute_target_share(season)
                    logger.info("Cached target_share %d", season)
                except Exception as exc:
                    logger.warning("Could not pre-load target_share %d: %s", season, exc)

            if f"weekly_{season}" not in self._data_cache:
                try:
                    self._data_cache[f"weekly_{season}"] = nfl_data.fetch_weekly_stats(season)
                    logger.info("Cached weekly_stats %d", season)
                except Exception as exc:
                    logger.warning("Could not pre-load weekly_stats %d: %s", season, exc)

            if f"ngs_receiving_{season}" not in self._data_cache:
                try:
                    raw = nfl_data.fetch_ngs_data("receiving", season)
                    self._data_cache[f"ngs_receiving_{season}"] = self._aggregate_ngs(
                        raw, ["avg_separation", "avg_yac_above_expectation"]
                    )
                    logger.info("Cached ngs_receiving %d", season)
                except Exception as exc:
                    logger.warning("Could not pre-load ngs_receiving %d: %s", season, exc)

            if f"ngs_rushing_{season}" not in self._data_cache:
                try:
                    raw = nfl_data.fetch_ngs_data("rushing", season)
                    self._data_cache[f"ngs_rushing_{season}"] = self._aggregate_ngs(
                        raw, ["rush_yards_over_expected_per_att", "rush_pct_over_expected"]
                    )
                    logger.info("Cached ngs_rushing %d", season)
                except Exception as exc:
                    logger.warning("Could not pre-load ngs_rushing %d: %s", season, exc)

        if f"rosters_{current_season}" not in self._data_cache:
            try:
                self._data_cache[f"rosters_{current_season}"] = nfl_data.fetch_rosters(current_season)
                logger.info("Cached rosters %d", current_season)
            except Exception as exc:
                logger.warning("Could not pre-load rosters %d: %s", current_season, exc)

        if f"snap_pct_{current_season}" not in self._data_cache:
            try:
                self._data_cache[f"snap_pct_{current_season}"] = nfl_data.compute_snap_pct(current_season)
                logger.info("Cached snap_pct %d", current_season)
            except Exception as exc:
                logger.warning("Could not pre-load snap_pct %d: %s", current_season, exc)

        logger.info(
            "Starting Player Profiles pipeline (concurrency=%d)", concurrency
        )
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, int] = {}

        async def _run_one(team: str) -> None:
            async with semaphore:
                results[team] = await self.run_for_team(team)

        await asyncio.gather(*[_run_one(t) for t in NFL_TEAMS])

        total = sum(results.values())
        logger.info("Player Profiles pipeline complete: %d total profiles written", total)
        return results


# ---------------------------------------------------------------------------
# Bulk DB write helpers
# ---------------------------------------------------------------------------

async def _bulk_resolve_player_ids(
    session: AsyncSession,
    names_and_teams: list[tuple[str, str]],
) -> dict[tuple, str | None]:
    """Resolve player IDs from (name, team) pairs in a single query."""
    results: dict[tuple, str | None] = {}
    unique_lasts = {n.split()[-1] for n, _ in names_and_teams if n}
    if not unique_lasts:
        return results

    conditions = [Player.name.ilike(f"%{last}%") for last in unique_lasts]
    all_players = (
        await session.execute(select(Player).where(or_(*conditions)))
    ).scalars().all()

    player_map: dict[str, list[Player]] = {}
    for p in all_players:
        last = p.name.split()[-1].lower()
        player_map.setdefault(last, []).append(p)

    for name, team in names_and_teams:
        if not name:
            results[(name, team)] = None
            continue
        last = name.split()[-1].lower()
        candidates = player_map.get(last, [])
        if not candidates:
            results[(name, team)] = None
        elif len(candidates) == 1:
            results[(name, team)] = str(candidates[0].id)
        else:
            match = [p for p in candidates if p.team_abbr and p.team_abbr.upper() == team.upper()]
            if not match:
                # No DB player on this team shares this last name.
                # Do NOT fall back to another team's player — that causes cross-team
                # profile writes (e.g. "Skyy Moore" for BUF resolving to KC's Skyy Moore).
                results[(name, team)] = None
            elif len(match) == 1:
                results[(name, team)] = str(match[0].id)
            else:
                # Multiple players on same team share a last name (e.g. Tyreek Hill vs Julian Hill).
                # Use full first name to pick the right record — first initial alone fails
                # when two same-team players share it (e.g. Carlos vs Casey Washington).
                first = name.split()[0].lower() if name else ""
                first_match = [
                    p for p in match
                    if p.name and p.name.split()[0].lower() == first
                ]
                results[(name, team)] = str((first_match or match)[0].id)

    return results


async def _write_profiles(
    profiles: list[dict], context: dict, team: str
) -> int:
    """Write player_profiles for one team — deletes stale records then inserts fresh ones."""
    if not profiles:
        return 0

    analysis_year = get_analysis_year()
    ctx_players   = context.get("players", [])

    # Build two lookups for finding the context player from model output:
    #   1. exact name  → ctx player dict
    #   2. normalized name → ctx player dict  (handles D.K./DK, Ja'Marr/Jamarr)
    ctx_by_name: dict[str, dict] = {p["name"]: p for p in ctx_players}
    ctx_by_norm: dict[str, dict] = {
        normalize_player_name(p["name"]): p for p in ctx_players
    }

    async with AsyncSessionLocal() as session:
        # Get all players for this team from DB to build ID resolution maps.
        team_players = (
            await session.execute(select(Player).where(Player.team_abbr == team))
        ).scalars().all()

        # Map nfl gsis player_id → DB uuid (most reliable cross-source key)
        nfl_id_to_db: dict[str, str] = {}
        for p in team_players:
            if p.yahoo_player_id and p.yahoo_player_id.startswith("nfl_"):
                nfl_id = p.yahoo_player_id[4:]  # strip "nfl_" prefix
                nfl_id_to_db[nfl_id] = str(p.id)

        # Map normalized name → DB uuid (fallback when no nfl_player_id available)
        name_to_db = build_player_lookup(
            [{"name": p.name, "id": str(p.id)} for p in team_players]
        )

        # Delete all existing profiles for this team before re-inserting.
        team_player_ids = [p.id for p in team_players]
        if team_player_ids:
            existing = (
                await session.execute(
                    select(PlayerProfile).where(
                        PlayerProfile.player_id.in_(team_player_ids)
                    )
                )
            ).scalars().all()
            for ep in existing:
                await session.delete(ep)
            if existing:
                logger.debug("%s: deleted %d stale profile(s) before rewrite", team, len(existing))

        written = 0
        written_ids: set = set()  # deduplicate: only first profile per player_id
        for prof in profiles:
            pname = prof.get("player_name", "")

            # --- Resolve context player (hallucination guard) ---
            ctx_player = ctx_by_name.get(pname) or ctx_by_norm.get(normalize_player_name(pname))
            if not ctx_player:
                logger.debug("Hallucinated (not in context): %s (%s)", pname, team)
                continue

            # --- Resolve DB UUID ---
            player_id: str | None = None
            # Primary: nfl gsis player_id passed through from roster
            nfl_pid = ctx_player.get("nfl_player_id")
            if nfl_pid:
                player_id = nfl_id_to_db.get(nfl_pid)
            # Fallback: normalized name lookup within this team's DB records
            if not player_id:
                player_id = name_to_db.get(normalize_player_name(pname))
            if not player_id:
                logger.debug("Could not resolve DB ID: %s (%s)", pname, team)
                continue

            if player_id in written_ids:
                logger.debug("Duplicate profile skipped: %s (%s)", pname, team)
                continue

            seasons    = ctx_player.get("seasons", [])
            ts3yr, ts_last, ay3yr = _compute_season_averages(seasons, analysis_year)

            # Route rookies to Python-computed rookie profile; veterans use model output
            # backed by Python-computed clean season baseline (most reliable).
            is_rookie = bool(ctx_player.get("is_rookie", False))

            if is_rookie:
                # Use purely Python-computed rookie profile — model has no NFL data to work with
                team_ctx = context.get("team_system", {})
                rookie_prof = _build_rookie_profile(ctx_player, team_ctx)
                clean_baseline = rookie_prof["clean_season_baseline"]
            elif ctx_player.get("position") == "QB":
                # QB baseline uses fantasy_points_ppr (includes passing scoring)
                clean_baseline = _compute_qb_baseline(seasons)
                rookie_prof    = {}
            else:
                # WR/RB/TE: compute clean_season_baseline in Python — do NOT trust model output.
                # PPR formula: receptions×1 + (rec_yards+rush_yards)×0.1 + tds×6
                clean_baseline = _compute_clean_baseline(seasons)
                rookie_prof    = {}

            # Always insert fresh (stale records for this team were deleted above)
            record = PlayerProfile(player_id=player_id, season_year=analysis_year)
            session.add(record)

            # Veteran fields — from model output (rookies use defaults from rookie_prof)
            effective = rookie_prof if is_rookie else prof
            record.role_classification        = effective.get("role_classification")
            record.separation_score           = effective.get("separation_score")
            record.yards_after_catch_score    = effective.get("yards_after_catch_score")
            record.efficiency_signal          = effective.get("efficiency_signal")
            record.age_curve_position         = effective.get("age_curve_position")
            record.career_trajectory          = effective.get("career_trajectory")
            # Use Python-computed baseline. If seasons are empty (e.g. player is
            # not in our WR/RB/TE context), set to empty dict rather than
            # falling back to the AI model's (possibly wrong) value.
            record.clean_season_baseline      = clean_baseline if clean_baseline else {}
            record.anomalous_seasons_excluded = effective.get("anomalous_seasons_excluded") or []
            record.breakout_flag              = bool(effective.get("breakout_flag", False))
            record.breakout_reasoning         = effective.get("breakout_reasoning")
            record.positional_scarcity_tier   = effective.get("positional_scarcity_tier")
            record.target_share_3yr_avg       = _to_decimal(ts3yr)
            record.target_share_last_season   = _to_decimal(ts_last)
            record.air_yards_share            = _to_decimal(ay3yr)
            record.snap_percentage            = _to_decimal(ctx_player.get("snap_pct"))

            # Rookie-specific columns
            record.is_rookie        = is_rookie
            record.profile_source   = "college_comps" if is_rookie else "nfl_history"
            record.confidence       = rookie_prof.get("confidence") if is_rookie else "medium"
            record.variance_flag    = bool(rookie_prof.get("variance_flag", False)) if is_rookie else False
            record.breakout_window  = rookie_prof.get("breakout_window") if is_rookie else None
            record.year1_role       = rookie_prof.get("year1_role") if is_rookie else None
            record.ceiling_value_ppr = _to_decimal(rookie_prof.get("ceiling_value_ppr")) if is_rookie else None
            record.floor_value_ppr   = _to_decimal(rookie_prof.get("floor_value_ppr")) if is_rookie else None

            # Update parent Player record
            player_row = (await session.execute(
                select(Player).where(Player.id == player_id)
            )).scalar_one_or_none()
            if player_row:
                player_row.breakout_flag   = bool(effective.get("breakout_flag", False))
                player_row.situation_score = effective.get("situation_score")

            written_ids.add(player_id)
            written += 1

        # --- Second pass: QBs not in model output get Python-only profiles ---
        for ctx_player in ctx_players:
            if ctx_player.get("position") != "QB":
                continue
            pname = ctx_player["name"]
            nfl_pid = ctx_player.get("nfl_player_id")

            # Resolve DB ID (same logic as above)
            player_id: str | None = None
            if nfl_pid:
                player_id = nfl_id_to_db.get(nfl_pid)
            if not player_id:
                player_id = name_to_db.get(normalize_player_name(pname))
            if not player_id or player_id in written_ids:
                continue  # already written above or can't resolve

            seasons = ctx_player.get("seasons", [])
            clean_baseline = _compute_qb_baseline(seasons)
            if not clean_baseline:
                continue  # not enough data

            record = PlayerProfile(player_id=player_id, season_year=analysis_year)
            session.add(record)
            record.role_classification = _derive_qb_role(clean_baseline)
            record.clean_season_baseline = clean_baseline
            record.anomalous_seasons_excluded = []
            record.is_rookie = bool(ctx_player.get("is_rookie", False))
            record.profile_source = "nfl_history"
            record.confidence = "medium"
            record.age_curve_position = _derive_qb_age_curve(ctx_player.get("age"))
            record.efficiency_signal = _derive_qb_efficiency(clean_baseline)

            written_ids.add(player_id)
            written += 1
            logger.debug("QB Python-only profile: %s (%s)", pname, team)

        await session.commit()

    return written


def _derive_qb_role(baseline: dict) -> str:
    """Derive QB role classification from PPG baseline."""
    ppg = baseline.get("ppg", 0)
    if ppg >= 22:
        return "qb_elite"
    if ppg >= 18:
        return "qb_starter"
    if ppg >= 14:
        return "qb_streamer"
    return "qb_backup"


def _derive_qb_age_curve(age: int | None) -> str:
    """Derive age curve position for QBs (peak 26-32)."""
    if age is None:
        return "peak"
    if age < 26:
        return "ascending"
    if age <= 32:
        return "peak"
    return "descending"


def _derive_qb_efficiency(baseline: dict) -> str:
    """Derive efficiency signal from QB PPG."""
    ppg = baseline.get("ppg", 0)
    if ppg >= 22:
        return "elite"
    if ppg >= 18:
        return "above_avg"
    if ppg >= 14:
        return "average"
    return "below_avg"


def _compute_season_averages(
    seasons: list[dict], analysis_year: int
) -> tuple[float | None, float | None, float | None]:
    """
    From a player's seasons list, compute:
      - 3yr avg target_share
      - last-season target_share
      - 3yr avg air_yards_share
    Only includes seasons before analysis_year with games > 0.
    """
    valid = [
        s for s in seasons
        if s.get("games", 0) > 0 and s.get("year", 0) < analysis_year
    ]
    if not valid:
        return None, None, None

    ts_vals = [s["target_share"] for s in valid if s.get("target_share") is not None]
    ay_vals  = [s["air_yards_share"] for s in valid if s.get("air_yards_share") is not None]
    sorted_valid = sorted(valid, key=lambda s: s.get("year", 0), reverse=True)
    ts_last = sorted_valid[0].get("target_share") if sorted_valid else None

    ts3yr = round(sum(ts_vals) / len(ts_vals), 3) if ts_vals else None
    ay3yr = round(sum(ay_vals) / len(ay_vals), 3) if ay_vals else None
    return ts3yr, ts_last, ay3yr


_MINIMUM_TOUCHES_FOR_PROJECTION = 50  # career receptions + carries across all seasons
_MINIMUM_QB_GAMES = 10  # minimum career games for QB projection


def _compute_qb_baseline(seasons: list[dict]) -> dict:
    """
    Compute clean_season_baseline for QBs using fantasy_points_ppr directly.

    QB PPR scoring includes passing (which the rec/rush formula doesn't capture):
        passing_td × 4 + passing_yards × 0.04 + INT × -2 +
        rushing_yards × 0.1 + rushing_td × 6 + receptions × 1

    Clean season = games >= 10.
    Minimum threshold: 10+ career games across all seasons.
    Career decline detection: same 65% rule as skill positions.
    """
    total_games = sum(s.get("games", 0) for s in seasons)
    if total_games < _MINIMUM_QB_GAMES:
        return {}

    clean = [s for s in seasons if s.get("games", 0) >= 10]
    if not clean:
        clean = [s for s in seasons if s.get("games", 0) > 0]
    if not clean:
        return {}

    def _season_ppg(s: dict) -> float:
        fp = s.get("fantasy_points_ppr") or s.get("ppr_per_game", 0)
        games = s.get("games", 1)
        if s.get("ppr_per_game"):
            return float(s["ppr_per_game"])
        return float(fp) / games if games > 0 else 0.0

    # Career decline detection
    sorted_clean = sorted(clean, key=lambda s: s.get("year", 0))
    season_ppgs = [_season_ppg(s) for s in sorted_clean]
    peak_ppg = max(season_ppgs) if season_ppgs else 0
    recent_ppg = season_ppgs[-1] if season_ppgs else 0

    is_declining = peak_ppg > 0 and recent_ppg < peak_ppg * 0.65

    if is_declining and len(sorted_clean) >= 2:
        career_ppg = sum(season_ppgs) / len(season_ppgs)
        avg_ppg = recent_ppg * 0.6 + career_ppg * 0.4
    else:
        avg_ppg = sum(season_ppgs) / len(season_ppgs)

    ppr_points = round(avg_ppg * 17, 1)  # 17-game projection

    # Passing stats averages
    pass_yds_pg = sum(
        s.get("passing_yards", 0) / max(s.get("games", 1), 1) for s in clean
    ) / len(clean)
    pass_tds_pg = sum(
        s.get("passing_tds", 0) / max(s.get("games", 1), 1) for s in clean
    ) / len(clean)
    avg_cpoe = None
    cpoe_vals = [s.get("cpoe") for s in clean if s.get("cpoe") is not None]
    if cpoe_vals:
        avg_cpoe = round(sum(cpoe_vals) / len(cpoe_vals), 2)

    result = {
        "ppr_points": ppr_points,
        "ppg": round(avg_ppg, 1),
        "passing_yards_pg": round(pass_yds_pg, 1),
        "passing_tds_pg": round(pass_tds_pg, 2),
    }
    if avg_cpoe is not None:
        result["cpoe"] = avg_cpoe
    if is_declining:
        result["declining"] = True
    return result


def _compute_clean_baseline(seasons: list[dict]) -> dict:
    """
    Compute clean_season_baseline as an average across clean seasons.

    Clean season = games >= 10 AND NOT backup_qb_season.
    Falls back to all seasons with games > 0 if no clean seasons exist.

    Minimum usage threshold: player must have at least 50 career touches
    (receptions + carries) to receive a projection. This prevents low-usage
    players (e.g. Jermar Jefferson with 21 career attempts) from getting
    inflated baselines.

    Career decline detection: if the most recent season's PPR is below 65%
    of the career peak, weight recent season at 60% and career average at 40%
    instead of flat averaging. This prevents aging/injured players (e.g. Chubb)
    from projecting at their peak.

    PPR formula (LEAGUE_RULES.md Rule #7):
        ppr_points = receptions × 1 + (rec_yards + rush_yards) × 0.1 + (rec_tds + rush_tds) × 6
    """
    # --- Minimum usage threshold ---
    total_receptions = sum(s.get("receptions", 0) for s in seasons)
    total_carries = sum(s.get("carries", 0) for s in seasons)
    if total_receptions + total_carries < _MINIMUM_TOUCHES_FOR_PROJECTION:
        return {}

    clean = [
        s for s in seasons
        if s.get("games", 0) >= 10 and not s.get("backup_qb_season", False)
    ]
    if not clean:
        clean = [s for s in seasons if s.get("games", 0) > 0]
    if not clean:
        return {}

    def _season_ppr(s: dict) -> float:
        rec = s.get("receptions", 0)
        rec_yds = s.get("rec_yards", 0)
        rec_td = s.get("rec_tds", 0)
        rush_yds = s.get("rush_yards", 0)
        rush_td = s.get("rush_tds", 0)
        return rec * 1.0 + (rec_yds + rush_yds) * 0.1 + (rec_td + rush_td) * 6.0

    # --- Career decline detection ---
    # Sort by year (most recent last) to identify recent vs peak
    sorted_clean = sorted(clean, key=lambda s: s.get("year", 0))
    season_pprs = [_season_ppr(s) for s in sorted_clean]
    peak_ppr = max(season_pprs) if season_pprs else 0
    recent_ppr = season_pprs[-1] if season_pprs else 0

    is_declining = peak_ppr > 0 and recent_ppr < peak_ppr * 0.65

    if is_declining and len(sorted_clean) >= 2:
        # Weight recent season 60%, career average 40%
        recent = sorted_clean[-1]
        career_n = len(sorted_clean)
        career_rec = sum(s.get("receptions", 0) for s in sorted_clean) / career_n
        career_rec_yards = sum(s.get("rec_yards", 0) for s in sorted_clean) / career_n
        career_rec_tds = sum(s.get("rec_tds", 0) for s in sorted_clean) / career_n
        career_rush_yards = sum(s.get("rush_yards", 0) for s in sorted_clean) / career_n
        career_rush_tds = sum(s.get("rush_tds", 0) for s in sorted_clean) / career_n

        rec = recent.get("receptions", 0) * 0.6 + career_rec * 0.4
        rec_yards = recent.get("rec_yards", 0) * 0.6 + career_rec_yards * 0.4
        rec_tds = recent.get("rec_tds", 0) * 0.6 + career_rec_tds * 0.4
        rush_yards = recent.get("rush_yards", 0) * 0.6 + career_rush_yards * 0.4
        rush_tds = recent.get("rush_tds", 0) * 0.6 + career_rush_tds * 0.4
    else:
        n = len(clean)
        rec = sum(s.get("receptions", 0) for s in clean) / n
        rec_yards = sum(s.get("rec_yards", 0) for s in clean) / n
        rec_tds = sum(s.get("rec_tds", 0) for s in clean) / n
        rush_yards = sum(s.get("rush_yards", 0) for s in clean) / n
        rush_tds = sum(s.get("rush_tds", 0) for s in clean) / n

    yards = rec_yards + rush_yards
    tds   = rec_tds + rush_tds
    ppr   = rec * 1.0 + yards * 0.1 + tds * 6.0

    result = {
        "receptions":  round(rec, 1),
        "yards":       round(yards, 1),
        "touchdowns":  round(tds, 1),
        "ppr_points":  round(ppr, 1),
    }
    if is_declining:
        result["declining"] = True
    return result


# ---------------------------------------------------------------------------
# Rookie profiling helpers (Step 5 — non-destructive addition)
# ---------------------------------------------------------------------------

_ROOKIE_CONFIDENCE_DISCOUNT: dict[str, float] = {
    "QB": 0.65,   # Most QBs take 2-3 years — highest uncertainty
    "WR": 0.75,   # Route running takes time against NFL coverage
    "TE": 0.70,   # Hardest college-to-NFL position transition
    "RB": 0.85,   # Translate fastest — contribute in Year 1
}

_ROOKIE_DEFAULT_PPG: dict[str, float] = {
    "QB": 16.0, "RB": 9.0, "WR": 9.5, "TE": 7.0
}

_DEVELOPMENT_TIMELINE: dict[str, str] = {
    "QB": "year_2_to_4",
    "WR": "year_2_to_3",
    "TE": "year_3_to_4",
    "RB": "year_1",
}


def _estimate_year1_role(player: dict, team_context: dict) -> str:
    capital    = player.get("draft_capital_signal", "medium")
    depth_rank = player.get("depth_chart_rank", 2)
    if capital == "high" and depth_rank == 1:
        return "starter"
    if capital == "high":
        return "rotational"
    if capital == "medium" and depth_rank <= 2:
        return "rotational"
    return "depth"


def _build_rookie_profile(player: dict, team_context: dict) -> dict:
    """
    Rookies are profiled from college data + historical comps pre-populated
    by Agent 2 (Roster Changes). Returns a profile dict compatible with
    the _write_profiles() PlayerProfile fields.
    """
    position = player.get("position", "WR")
    base_ppg = player.get("comp_yr1_avg_ppg") or _ROOKIE_DEFAULT_PPG.get(position, 8.0)

    # Adjust by landing spot modifier (0.75 compound risk → 1.18 elite system)
    landing_modifier = float(player.get("landing_spot_modifier") or 1.0)
    adjusted_ppg     = base_ppg * landing_modifier

    # Season projection at 17 games, then apply confidence discount
    projected_season = adjusted_ppg * 17
    discount         = _ROOKIE_CONFIDENCE_DISCOUNT.get(position, 0.75)
    discounted       = projected_season * discount

    # Wider ceiling/floor than veterans (genuine uncertainty — not pessimism)
    ceiling = discounted * 1.45
    floor   = discounted * 0.55

    # Elite breakout: Ja'Marr Chase / Justin Jefferson tier
    is_breakout = (
        player.get("college_profile_grade") == "elite"
        and player.get("draft_capital_signal") == "high"
    )

    return {
        # Fields overlapping with veteran profile schema
        "is_rookie":               True,
        "profile_source":          "college_comps",
        "clean_season_baseline":   {
            "ppr_points": round(discounted, 1),
            "note":       "Derived from historical comp average — not NFL history",
        },
        "ceiling_value_ppr":       round(ceiling, 1),
        "floor_value_ppr":         round(floor, 1),
        "confidence":              "low",
        "variance_flag":           True,
        "college_profile_grade":   player.get("college_profile_grade"),
        "draft_capital_signal":    player.get("draft_capital_signal"),
        "historical_comp_names":   player.get("historical_comp_names", []),
        "comp_yr1_avg_ppg":        player.get("comp_yr1_avg_ppg"),
        "comp_yr2_avg_ppg":        player.get("comp_yr2_avg_ppg"),
        "landing_spot_modifier":   landing_modifier,
        "breakout_window":         _DEVELOPMENT_TIMELINE.get(position),
        "year1_role":              _estimate_year1_role(player, team_context),
        "breakout_flag":           is_breakout,
        "breakout_reasoning":      (
            "Elite college profile + high draft capital = Year 1 upside (Chase/Jefferson tier)"
            if is_breakout else None
        ),
        "anomalous_seasons_excluded": [],   # N/A for rookies
        # Veteran-only fields — set to neutral defaults
        "role_classification":     None,
        "separation_score":        "avg",
        "yards_after_catch_score": "avg",
        "efficiency_signal":       "avg",
        "age_curve_position":      "ascending",
        "career_trajectory":       "volatile",
        "positional_scarcity_tier": None,
        "situation_score":         "volatile",
    }


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 3)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Module-level compatibility shims
# ---------------------------------------------------------------------------

_agent_instance: PlayerProfilesAgent | None = None


def _get_agent(dry_run: bool = False) -> PlayerProfilesAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = PlayerProfilesAgent(dry_run=dry_run)
    return _agent_instance


async def run_for_team(team_abbr: str, dry_run: bool = False) -> int:
    return await _get_agent(dry_run).run_for_team(team_abbr)


async def run_all_teams(concurrency: int = 4, dry_run: bool = False) -> dict[str, int]:
    return await _get_agent(dry_run).run_all_teams(concurrency)
