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
from backend.models.player import Player, PlayerProfile
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

logger = logging.getLogger(__name__)

SKILL_POSITIONS = {"WR", "RB", "TE"}

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
- Age curve peaks: RB 24-26, WR 24-29, TE 26-29. ascending = before peak; descending = past peak.
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
    AGENT_MAX_TOKENS = 1000

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
        self, player_name: str, team: str, season: int
    ) -> dict | None:
        """Return compact season stats for one player from the cached target_share df."""
        ts_df = self._data_cache.get(f"target_share_{season}")
        if ts_df is None:
            return None

        last = player_name.split()[-1]
        mask = (
            ts_df["player_name"].str.contains(last, case=False, na=False) &
            (ts_df["recent_team"] == team)
        )
        rows = ts_df[mask]
        if rows.empty:
            # Player may have been on a different team in this season (pre-trade).
            # Fall back to any-team match so historical baselines include all seasons.
            # Sort by games desc so the most-played player wins (avoids wrong-name collisions).
            rows = (
                ts_df[ts_df["player_name"].str.contains(last, case=False, na=False)]
                .sort_values("games", ascending=False)
            )
        if rows.empty:
            return None

        row   = rows.iloc[0]
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
            "games":          games,
            "recent_team":    str(row.get("recent_team", "") or ""),
            "target_share":   _f("avg_target_share"),
            "air_yards_share": _f("avg_air_yards_share"),
            "targets":        int(row.get("total_targets",    0) or 0),
            "receptions":     int(row.get("total_receptions", 0) or 0),
            "rec_yards":      int(row.get("total_rec_yards",  0) or 0),
            "rec_tds":        int(row.get("total_rec_tds",    0) or 0),
            "carries":        int(row.get("total_carries",    0) or 0),
            "rush_yards":     int(row.get("total_rush_yards", 0) or 0),
            "rush_tds":       int(row.get("total_rush_tds",   0) or 0),
            "ppr_per_game":   _f("ppr_per_game", 1),
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

            seasons_data: list[dict] = []
            for season in analysis_seasons:
                stats = self._get_player_season_stats(pname, team, season)
                if stats:
                    stats["year"]             = season
                    # Only apply backup_qb flag when stats are from the current team.
                    # Pre-trade seasons used the player's old team QB, not this team's.
                    stat_team = stats.get("recent_team", team)
                    stats["backup_qb_season"] = (
                        backup_qb_flags.get(season, False)
                        if stat_team.upper() == team.upper()
                        else False
                    )
                    # Attach NGS efficiency data per position
                    pos = info["position"]
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
            results[(name, team)] = str(match[0].id) if match else str(candidates[0].id)

    return results


async def _write_profiles(
    profiles: list[dict], context: dict, team: str
) -> int:
    """Bulk upsert player_profiles — one DB transaction per team."""
    if not profiles:
        return 0

    analysis_year = get_analysis_year()
    ctx_map: dict[str, dict] = {
        p["name"]: p for p in context.get("players", [])
    }

    async with AsyncSessionLocal() as session:
        names_and_teams = [(p.get("player_name", ""), team) for p in profiles]
        id_map = await _bulk_resolve_player_ids(session, names_and_teams)

        written = 0
        for prof in profiles:
            pname     = prof.get("player_name", "")
            player_id = id_map.get((pname, team))
            if not player_id:
                logger.debug("Could not resolve player: %s (%s)", pname, team)
                continue

            ctx_player = ctx_map.get(pname, {})
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
            else:
                # Compute clean_season_baseline in Python — do NOT trust model output.
                # Rule: average across seasons with games >= 10 and not backup_qb_season.
                # PPR formula: receptions×1 + (rec_yards+rush_yards)×0.1 + tds×6
                clean_baseline = _compute_clean_baseline(seasons)
                rookie_prof    = {}

            # Upsert PlayerProfile
            existing = (await session.execute(
                select(PlayerProfile).where(
                    PlayerProfile.player_id == player_id,
                    PlayerProfile.season_year == analysis_year,
                )
            )).scalar_one_or_none()

            if existing:
                record = existing
            else:
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

            written += 1

        await session.commit()

    return written


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


def _compute_clean_baseline(seasons: list[dict]) -> dict:
    """
    Compute clean_season_baseline as an average across clean seasons.

    Clean season = games >= 10 AND NOT backup_qb_season.
    Falls back to all seasons with games > 0 if no clean seasons exist.

    PPR formula (LEAGUE_RULES.md Rule #7):
        ppr_points = receptions × 1 + (rec_yards + rush_yards) × 0.1 + (rec_tds + rush_tds) × 6
    """
    clean = [
        s for s in seasons
        if s.get("games", 0) >= 10 and not s.get("backup_qb_season", False)
    ]
    if not clean:
        clean = [s for s in seasons if s.get("games", 0) > 0]
    if not clean:
        return {}

    n = len(clean)
    rec       = sum(s.get("receptions", 0) for s in clean) / n
    rec_yards = sum(s.get("rec_yards",  0) for s in clean) / n
    rec_tds   = sum(s.get("rec_tds",    0) for s in clean) / n
    rush_yards = sum(s.get("rush_yards", 0) for s in clean) / n
    rush_tds   = sum(s.get("rush_tds",  0) for s in clean) / n

    yards = rec_yards + rush_yards
    tds   = rec_tds + rush_tds
    ppr   = rec * 1.0 + yards * 0.1 + tds * 6.0

    return {
        "receptions":  round(rec, 1),
        "yards":       round(yards, 1),
        "touchdowns":  round(tds, 1),
        "ppr_points":  round(ppr, 1),
    }


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
