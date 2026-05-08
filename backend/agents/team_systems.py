"""
Agent 1: Team Systems Agent

Grades every NFL team's offensive system for the upcoming season.
Runs first in the pre-draft pipeline — output is inherited by all other agents.

Architecture:
  - Model: Haiku (data extraction, not reasoning)
  - Max tokens: 500 per team
  - Pattern: pre-aggregate in Python → ONE call_once() per team → parse JSON → write DB
  - Never uses run_agent() (that is for live draft only)

Key flags produced:
  - rookie_qb_flag: true for any first-year starter
  - compound_risk_flag: rookie QB AND pass_protection_grade C or below
    → cascades as a severe penalty to all skill positions on that roster
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU
from backend.database import AsyncSessionLocal
from backend.integrations import nfl_data
from backend.models.team_system import TeamSystem
from backend.utils.seasons import get_current_season, get_analysis_seasons

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# All 32 NFL teams
# ---------------------------------------------------------------------------

NFL_TEAMS: list[str] = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB",  "HOU", "IND", "JAX", "KC",
    "LA",  "LAC", "LV",  "MIA", "MIN", "NE",  "NO",  "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF",  "TB",  "TEN", "WAS",
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an NFL offensive system analyst building a pre-draft fantasy football research database.

Your job is to grade one NFL team's offensive system for the upcoming season.
You will receive pre-aggregated real data. Use it alongside your own knowledge of
current rosters, coaching staff, and known offseason changes.

Produce a JSON object matching this exact schema:
{
  "team_abbr": "string (3-5 char NFL team code)",
  "pass_protection_grade": "string (A+/A/A-/B+/B/B-/C+/C/C-/D+/D/F)",
  "run_blocking_grade": "string (A+/A/A-/B+/B/B-/C+/C/C-/D+/D/F)",
  "qb_name": "string",
  "qb_tier": "string (elite/solid/average/weak/rookie)",
  "qb_experience_years": integer,
  "qb_pressure_performance": "string (elite/above_avg/avg/below_avg)",
  "qb_cpoe": number,
  "qb_air_yards_per_attempt": number,
  "qb_downfield_aggressiveness": "string (aggressive/moderate/conservative)",
  "rookie_qb_flag": boolean,
  "compound_risk_flag": boolean,
  "oc_name": "string",
  "oc_scheme": "string (balanced/pass_heavy/run_heavy/west_coast/air_raid/spread)",
  "oc_run_pass_split_tendency": number,
  "personnel_tendency": "string (11/12/21/22/13)",
  "red_zone_philosophy": "string (wr1/te/rb/spread/qb_scramble)",
  "system_ceiling": "string (high/moderate/low)",
  "system_grade": "string (A+/A/A-/B+/B/B-/C+/C/C-/D+/D/F)",
  "notes": "string (2-3 sentences, fantasy implications focus)"
}

Rules:
- rookie_qb_flag = true if this is the QB's first full season as a starter
- compound_risk_flag = true ONLY when rookie_qb_flag is true AND pass_protection_grade is C or below
- compound_risk_flag cascades severe penalties to all skill position players — flag conservatively
- The notes field must focus on fantasy implications, not general football analysis

Output ONLY a valid JSON object. No explanation. No preamble. No markdown fences.
Your entire response must be parseable by json.loads()."""


# ---------------------------------------------------------------------------
# TeamSystemsAgent
# ---------------------------------------------------------------------------

class TeamSystemsAgent(BaseAgent):
    AGENT_NAME       = "team_systems"
    AGENT_MODEL      = HAIKU
    AGENT_MAX_TOKENS = 500

    # Shared data cache — loaded once in run_all_teams(), reused per team
    _data_cache: ClassVar[dict] = {}

    # ------------------------------------------------------------------
    # Data pre-aggregation — all Python, zero API calls
    # ------------------------------------------------------------------

    async def _build_team_context(self, team: str) -> dict:
        """
        Pre-fetch and aggregate ALL data for one team.
        Returns a compact dict ready to pass to the model.
        No API calls here — only nfl_data_py and DB lookups.
        """
        current_season = get_current_season()
        analysis_seasons = get_analysis_seasons(3)

        # Use most recent season with available weekly data for stats.
        # current_season (2025) may not have data yet if season hasn't started.
        stats_season = current_season
        if f"weekly_{current_season}" not in self._data_cache:
            # Fall back to most recent available
            for s in sorted(analysis_seasons, reverse=True):
                if f"weekly_{s}" in self._data_cache:
                    stats_season = s
                    break

        oline = await self._get_oline_data(team, stats_season)
        qb    = await self._get_qb_data(team, stats_season)
        pers  = await self._get_personnel_data(team, stats_season)
        roster = await self._get_roster_summary(team, current_season)

        return {
            "team": team,
            "season": current_season,
            "oline": oline,
            "qb_metrics": qb,
            "personnel": pers,
            "roster_summary": roster,
            "instruction": (
                f"Grade the {team} offensive system for the {current_season + 1} NFL season. "
                f"Use the pre-aggregated data above and your knowledge of this team's "
                f"current OC, QB situation, and any known offseason changes."
            ),
        }

    async def _get_oline_data(self, team: str, season: int) -> dict:
        cache_key = f"weekly_{season}"
        weekly = self._data_cache.get(cache_key)
        if weekly is None:
            try:
                weekly = nfl_data.fetch_weekly_stats(season)
                self._data_cache[cache_key] = weekly
            except Exception as exc:
                logger.warning("Could not load weekly stats %d: %s", season, exc)
                return {"team": team, "note": "No data — use model knowledge"}

        team_df = weekly[weekly["recent_team"] == team]
        if team_df.empty:
            return {"team": team, "note": "No data — use model knowledge"}

        total_attempts = team_df["attempts"].sum()
        total_sacks    = team_df["sacks"].sum() if "sacks" in team_df.columns else 0
        # Dropbacks = attempts + sacks (sacks don't count as pass attempts)
        total_dropbacks = int(total_attempts + total_sacks)
        sack_rate      = float(total_sacks / total_dropbacks) if total_dropbacks > 0 else None

        # Get avg_time_to_throw from pre-loaded oline stats
        avg_ttt = None
        oline_stats = self._data_cache.get(f"oline_stats_{season}")
        if oline_stats is not None and not oline_stats.empty:
            import pandas as pd
            team_row = oline_stats[oline_stats["team"] == team]
            if not team_row.empty:
                ttt_val = team_row.iloc[0].get("avg_time_to_throw")
                if ttt_val is not None and pd.notna(ttt_val):
                    avg_ttt = round(float(ttt_val), 3)

        return {
            "team": team,
            "season": season,
            "total_dropbacks": total_dropbacks,
            "sack_rate": round(sack_rate, 4) if sack_rate is not None else None,
            "avg_time_to_throw": avg_ttt,
            "note": (
                "Use sack_rate and avg_time_to_throw as primary inputs for pass_protection_grade. "
                "Lower sack_rate (<5%) and moderate time_to_throw (~2.6-2.8s) indicate good protection."
            ),
        }

    async def _get_qb_data(self, team: str, season: int) -> dict:
        """
        Two-source QB identification:
        1. Seasonal roster (current_season) → who IS the QB
        2. Weekly stats (stats_season) → pull that QB's stats across ALL teams
        3. Fallback: most passing attempts on this team (legacy behavior)
        """
        from backend.integrations.nfl_data import normalize_player_name

        current_season = get_current_season()

        # --- Source 1: Identify current QB from seasonal roster ---
        roster_key = f"seasonal_rosters_{current_season}"
        roster = self._data_cache.get(roster_key)

        starter_name = None
        if roster is not None and not roster.empty:
            team_qbs = roster[
                (roster["team"] == team)
                & (roster["position"] == "QB")
                & (roster["status"] == "ACT")
            ]
            if not team_qbs.empty:
                starter_name = team_qbs.iloc[0]["player_name"]

        # --- Source 2: Weekly stats ---
        cache_key = f"weekly_{season}"
        weekly = self._data_cache.get(cache_key)
        if weekly is None:
            try:
                weekly = nfl_data.fetch_weekly_stats(season)
                self._data_cache[cache_key] = weekly
            except Exception as exc:
                logger.warning("Could not load weekly stats %d: %s", season, exc)
                if starter_name:
                    return {
                        "team": team, "season": season,
                        "starter_name": starter_name,
                        "source": "roster_only",
                        "note": f"{starter_name} identified from roster but no weekly stats available — use model knowledge",
                    }
                return {"team": team, "note": "No QB data — use model knowledge"}

        all_qb_data = weekly[weekly["position"] == "QB"]

        if starter_name:
            # Search by normalized name across ALL teams
            norm_target = normalize_player_name(starter_name)
            qb_copy = all_qb_data.copy()
            qb_copy["_norm"] = qb_copy["player_name"].apply(normalize_player_name)
            starter_df = qb_copy[qb_copy["_norm"] == norm_target]

            # Log QB change if roster QB differs from stats leader on this team
            team_qb_stats = all_qb_data[all_qb_data["recent_team"] == team]
            if not team_qb_stats.empty:
                stats_leader = (
                    team_qb_stats.groupby("player_name")["attempts"]
                    .sum()
                    .sort_values(ascending=False)
                    .index[0]
                )
                if normalize_player_name(stats_leader) != norm_target:
                    logger.info(
                        "QB CHANGE: %s — roster=%s, stats_leader=%s",
                        team, starter_name, stats_leader,
                    )

            if starter_df.empty:
                # Roster says this is the QB but no stats found (true rookie, etc.)
                return {
                    "team": team, "season": season,
                    "starter_name": starter_name,
                    "source": "roster_only",
                    "note": f"{starter_name} identified from roster but no stats found — use model knowledge",
                }
        else:
            # --- Fallback: most passing attempts on this team ---
            team_qb_data = all_qb_data[all_qb_data["recent_team"] == team]
            if team_qb_data.empty:
                return {"team": team, "note": "No QB data — use model knowledge"}
            starter_name = (
                team_qb_data.groupby("player_name")["attempts"]
                .sum()
                .sort_values(ascending=False)
                .index[0]
            )
            starter_df = team_qb_data[team_qb_data["player_name"] == starter_name]

        total_att = starter_df["attempts"].sum()

        return {
            "team": team,
            "season": season,
            "starter_name": starter_name,
            "source": "roster+stats" if roster is not None else "stats_fallback",
            "games_played": int(len(starter_df)),
            "total_attempts": int(total_att),
            "completion_pct": (
                round(float(starter_df["completions"].sum() / total_att), 3)
                if total_att > 0 else None
            ),
            "passing_yards": int(starter_df["passing_yards"].sum()),
            "passing_tds": int(starter_df["passing_tds"].sum()),
            "interceptions": int(starter_df["interceptions"].sum()),
            "air_yards_per_attempt": (
                round(float(starter_df["passing_air_yards"].sum() / total_att), 2)
                if total_att > 0 else None
            ),
            "rushing_yards": int(starter_df["rushing_yards"].sum()) if "rushing_yards" in starter_df.columns else 0,
            "rushing_tds": int(starter_df["rushing_tds"].sum()) if "rushing_tds" in starter_df.columns else 0,
            "dakota": (
                round(float(starter_df["dakota"].mean()), 3)
                if "dakota" in starter_df.columns and not starter_df["dakota"].isna().all()
                else None
            ),
            "note": "Supplement with your knowledge of this QB's performance under pressure and CPOE.",
        }

    async def _get_personnel_data(self, team: str, season: int) -> dict:
        cache_key = f"weekly_{season}"
        weekly = self._data_cache.get(cache_key)
        if weekly is None:
            try:
                weekly = nfl_data.fetch_weekly_stats(season)
                self._data_cache[cache_key] = weekly
            except Exception as exc:
                logger.warning("Could not load weekly stats %d: %s", season, exc)
                return {"team": team, "note": "No data — use model knowledge"}

        skill = weekly[
            (weekly["recent_team"] == team) &
            (weekly["position"].isin(["WR", "TE", "RB"]))
        ]
        if skill.empty:
            return {"team": team, "note": "No skill position data"}

        pos_targets = skill.groupby("position")["targets"].sum()
        total_targets = pos_targets.sum()
        target_share_by_pos = {
            str(pos): round(float(t / total_targets), 3)
            for pos, t in pos_targets.items()
        } if total_targets > 0 else {}

        top_scorers = (
            skill.groupby(["player_name", "position"])["receiving_tds"]
            .sum()
            .sort_values(ascending=False)
            .head(5)
            .reset_index()
            .to_dict(orient="records")
        )

        return {
            "team": team,
            "season": season,
            "target_share_by_position": target_share_by_pos,
            "top_td_scorers": top_scorers,
            "note": "Supplement with your knowledge of 11/12/21 personnel rates and red zone tendencies.",
        }

    async def _get_roster_summary(self, team: str, season: int) -> dict:
        cache_key = f"rosters_{season}"
        rosters = self._data_cache.get(cache_key)
        if rosters is None:
            try:
                rosters = nfl_data.fetch_rosters(season)
                self._data_cache[cache_key] = rosters
            except Exception as exc:
                logger.warning("Could not load rosters %d: %s", season, exc)
                return {"team": team, "note": "No roster data"}

        team_roster = rosters[rosters["team"] == team]
        skill = team_roster[
            team_roster["position"].isin(["QB", "RB", "WR", "TE", "OL", "T", "G", "C"])
        ][["player_name", "position", "depth_chart_position", "status"]].head(25)

        return {"team": team, "roster": skill.to_dict(orient="records")}

    # ------------------------------------------------------------------
    # Per-team runner — exactly ONE call_once()
    # ------------------------------------------------------------------

    async def run_for_team(self, team_abbr: str) -> dict | None:
        """Run agent for one team. Returns parsed JSON dict or None on failure."""
        team = team_abbr.upper()
        logger.info("Building context for %s", team)

        try:
            context = await self._build_team_context(team)

            raw = await self.call_once(
                system=SYSTEM_PROMPT,
                user=json.dumps(context, default=str),
                input_data=context,
                entity_id=team,
            )

            if not raw:
                return None  # dry_run or cache miss returned empty

            data = parse_json_output(raw)
            if not isinstance(data, dict):
                logger.error("Non-dict output for %s: %s", team, raw[:200])
                return None

            # Enforce team_abbr from our canonical list
            data["team_abbr"] = team

            # Attach Python-computed numerics (NOT from model output)
            oline_data = context.get("oline", {})
            qb_data = context.get("qb_metrics", {})
            data["_sack_rate"] = oline_data.get("sack_rate")
            data["_avg_time_to_throw"] = oline_data.get("avg_time_to_throw")
            data["_qb_mobility"] = _derive_qb_mobility(qb_data)

            async with AsyncSessionLocal() as session:
                await _upsert_team_system(session, data)

            return data

        except Exception as exc:
            logger.error("Team Systems Agent failed for %s: %s", team, exc, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Full pipeline — pre-warm caches once, then run all 32 teams
    # ------------------------------------------------------------------

    async def run_all_teams(self, concurrency: int = 4) -> dict[str, bool]:
        """
        Run for all 32 teams with bounded concurrency.
        Pre-warms shared data caches BEFORE starting concurrent team runs
        so each team call slices from pre-loaded data instead of re-downloading.
        Returns {team_abbr: success_bool}.
        """
        current_season = get_current_season()
        analysis_seasons = get_analysis_seasons(3)

        logger.info(
            "Pre-loading NFL data caches for seasons %s...", analysis_seasons
        )
        for season in analysis_seasons:
            cache_key = f"weekly_{season}"
            if cache_key not in self._data_cache:
                try:
                    self._data_cache[cache_key] = nfl_data.fetch_weekly_stats(season)
                    logger.info("Cached weekly stats %d", season)
                except Exception as exc:
                    logger.warning("Could not pre-load weekly stats %d: %s", season, exc)

            # Pre-load oline stats (sack_rate + avg_time_to_throw)
            oline_key = f"oline_stats_{season}"
            if oline_key not in self._data_cache:
                try:
                    self._data_cache[oline_key] = nfl_data.compute_team_oline_stats(season)
                    logger.info("Cached oline stats %d", season)
                except Exception as exc:
                    logger.warning("Could not pre-load oline stats %d: %s", season, exc)

        rosters_key = f"rosters_{current_season}"
        if rosters_key not in self._data_cache:
            try:
                self._data_cache[rosters_key] = nfl_data.fetch_rosters(current_season)
                logger.info("Cached rosters %d", current_season)
            except Exception as exc:
                logger.warning("Could not pre-load rosters %d: %s", current_season, exc)

        # Seasonal rosters — used by _get_qb_data() for current QB identity
        seasonal_key = f"seasonal_rosters_{current_season}"
        if seasonal_key not in self._data_cache:
            try:
                self._data_cache[seasonal_key] = nfl_data.fetch_seasonal_rosters(current_season)
                logger.info("Cached seasonal rosters %d", current_season)
            except Exception as exc:
                logger.warning("Could not pre-load seasonal rosters %d: %s", current_season, exc)

        logger.info(
            "Starting Team Systems pipeline for all 32 teams (concurrency=%d)", concurrency
        )
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, bool] = {}

        async def _run_one(team: str) -> None:
            async with semaphore:
                data = await self.run_for_team(team)
                results[team] = data is not None

        await asyncio.gather(*[_run_one(t) for t in NFL_TEAMS])

        success = sum(1 for v in results.values() if v)
        logger.info("Team Systems pipeline complete: %d/32 teams successful", success)
        return results


# ---------------------------------------------------------------------------
# QB mobility helper
# ---------------------------------------------------------------------------


def _derive_qb_mobility(qb_data: dict) -> str | None:
    """
    Classify QB mobility from rushing stats.
    > 40 rush yards/game = elite (Lamar, Hurts)
    15-40 = average
    < 15 = pocket_only
    """
    games = qb_data.get("games_played", 0)
    rush_yards = qb_data.get("rushing_yards", 0) if isinstance(qb_data.get("rushing_yards"), (int, float)) else 0
    if not games or games < 5:
        return None
    rush_ypg = rush_yards / games
    if rush_ypg > 40:
        return "elite"
    if rush_ypg >= 15:
        return "average"
    return "pocket_only"


# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------

async def _upsert_team_system(session: AsyncSession, data: dict) -> None:
    current_season = get_current_season()
    team = data.get("team_abbr", "").upper()

    result = await session.execute(
        select(TeamSystem).where(
            TeamSystem.team_abbr == team,
            TeamSystem.season_year == current_season,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        record = existing
    else:
        record = TeamSystem(team_abbr=team, season_year=current_season)
        session.add(record)

    record.pass_protection_grade       = data.get("pass_protection_grade")
    record.run_blocking_grade          = data.get("run_blocking_grade")
    record.qb_name                     = data.get("qb_name")
    record.qb_tier                     = data.get("qb_tier")
    record.qb_experience_years         = data.get("qb_experience_years")
    record.qb_pressure_performance     = data.get("qb_pressure_performance")
    record.qb_cpoe                     = data.get("qb_cpoe")
    record.qb_air_yards_per_attempt    = data.get("qb_air_yards_per_attempt")
    record.qb_downfield_aggressiveness = data.get("qb_downfield_aggressiveness")
    record.rookie_qb_flag              = bool(data.get("rookie_qb_flag", False))
    record.compound_risk_flag          = bool(data.get("compound_risk_flag", False))
    record.oc_name                     = data.get("oc_name")
    record.oc_scheme                   = data.get("oc_scheme")
    _split = data.get("oc_run_pass_split_tendency")
    if _split is not None and _split > 1:
        _split = round(_split / 100, 3)  # model returned 45 instead of 0.45
    record.oc_run_pass_split_tendency  = _split
    record.personnel_tendency          = data.get("personnel_tendency")
    record.red_zone_philosophy         = data.get("red_zone_philosophy")
    record.system_ceiling              = data.get("system_ceiling")
    record.system_grade                = data.get("system_grade")
    record.notes                       = data.get("notes")

    # Python-computed numerics (prefixed with _ in data dict)
    record.sack_rate                   = data.get("_sack_rate")
    record.avg_time_to_throw           = data.get("_avg_time_to_throw")
    record.qb_mobility                 = data.get("_qb_mobility")

    await session.commit()
    logger.info(
        "Upserted TeamSystem: %s — Grade: %s  rookie_qb=%s  compound_risk=%s",
        team, record.system_grade, record.rookie_qb_flag, record.compound_risk_flag,
    )


# ---------------------------------------------------------------------------
# Module-level compatibility shims (used by pipeline.py and scripts)
# ---------------------------------------------------------------------------

_agent_instance: TeamSystemsAgent | None = None


def _get_agent(dry_run: bool = False) -> TeamSystemsAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = TeamSystemsAgent(dry_run=dry_run)
    return _agent_instance


async def run_for_team(team_abbr: str, dry_run: bool = False) -> dict | None:
    return await _get_agent(dry_run).run_for_team(team_abbr)


async def run_all_teams(concurrency: int = 4, dry_run: bool = False) -> dict[str, bool]:
    return await _get_agent(dry_run).run_all_teams(concurrency)
