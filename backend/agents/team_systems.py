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
  - rookie_qb_flag: true only for genuine first-time NFL starters (not veterans on new teams)
  - compound_risk_flag: rookie QB AND pass_protection_grade C or below
    → cascades as a severe penalty to all skill positions on that roster
"""
from __future__ import annotations

import asyncio
import json
import logging

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU
from backend.database import AsyncSessionLocal
from backend.integrations.nfl_data import normalize_player_name
from backend.models.team_system import TeamSystem
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OLine draft context — runtime injection only (no DB storage)
# ---------------------------------------------------------------------------

_OLINE_POSITIONS = frozenset({"T", "G", "C", "OT", "OG", "OL", "OC"})

# nfl_data_py uses PFR team codes — map to our canonical abbreviations
_PFR_TEAM_MAP: dict[str, str] = {
    "GNB": "GB", "KAN": "KC", "LAR": "LA", "LVR": "LV",
    "NOR": "NO", "NWE": "NE", "SFO": "SF", "TAM": "TB",
}
_CANONICAL_TO_PFR: dict[str, str] = {v: k for k, v in _PFR_TEAM_MAP.items()}

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
- rookie_qb_flag = true ONLY if this QB has never been a full-season NFL starter before — meaning a true first or second-year player making their debut as a starter.
  TRUE examples (actual rookies/first-time starters): Shedeur Sanders (CLE), Jaxson Dart (NYG), Cam Ward (TEN).
  FALSE examples (veterans on new teams): Kyler Murray (6+ NFL seasons), Gardner Minshew (6+ seasons), Spencer Rattler (1+ seasons, NFL veteran), Bo Nix (played a full prior season as starter).
  A QB who changes teams is NOT a rookie. A QB who missed time due to injury is NOT a rookie. rookie_qb_flag is about NFL experience level, not familiarity with a new team or system.
- compound_risk_flag = true ONLY when rookie_qb_flag is true AND pass_protection_grade is C or below. This flag is reserved for genuine first-year starters behind bad OLines — a rare scenario. Flag conservatively.
- The notes field must focus on fantasy implications, not general football analysis

OLine grade adjustment rules (when oline_draft_picks is provided in oline data):
- Round 1 OT pick (picks 1-32): pass_protection_grade improves 1-2 grades above historical sack rate baseline.
  Example: 8% sack rate = C+ baseline, but R1 OT pick → B or B+.
- Round 1 OG/C pick (picks 1-32): run_blocking_grade improves 1-2 grades. Modest pass_protection improvement too.
- Round 2 OT/OG (picks 33-64): improve grades by 1 grade. Day 1 starter likely but not guaranteed.
- Round 3+ OLine picks: modest improvement, depth/rotational. 0-1 grade improvement maximum.
- Multiple OLine picks same year: stack improvements (additive). 2 OLinemen in R1-R2 = full grade adjustments.
- No OLine picks + historical sack_rate > 7%: grade stays at or near historical level. Do not inflate without evidence.
- When oline_draft_picks is empty []: base grade entirely on sack_rate and avg_time_to_throw.

Output ONLY a valid JSON object. No explanation. No preamble. No markdown fences.
Your entire response must be parseable by json.loads()."""


# ---------------------------------------------------------------------------
# TeamSystemsAgent
# ---------------------------------------------------------------------------

class TeamSystemsAgent(BaseAgent):
    AGENT_NAME       = "team_systems"
    AGENT_MODEL      = HAIKU
    AGENT_MAX_TOKENS = 500

    # Class-level cache: fetched once, shared across all 32 teams
    _draft_cache: dict[int, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # OLine draft context — fetched once per season, not per team
    # ------------------------------------------------------------------

    def _get_oline_draft_context(self, team: str, season: int) -> list[dict]:
        """Return OLine draft picks for *team* in the given draft *season*.

        Uses a class-level cache so nfl_data_py is called at most once
        across all 32 teams.  Returns empty list on miss.
        """
        if season not in self.__class__._draft_cache:
            try:
                import nfl_data_py as nfl_data
                draft = nfl_data.import_draft_picks([season])
                self.__class__._draft_cache[season] = draft
            except Exception as exc:
                logger.warning("Draft picks %d unavailable: %s", season, exc)
                self.__class__._draft_cache[season] = pd.DataFrame()

        draft = self.__class__._draft_cache[season]
        if draft.empty:
            return []

        # Map canonical team code to PFR code used by nfl_data_py
        pfr_team = _CANONICAL_TO_PFR.get(team, team)

        team_oline = draft[
            (draft["team"] == pfr_team)
            & (draft["position"].isin(_OLINE_POSITIONS))
        ].sort_values("pick")

        name_col = "pfr_player_name" if "pfr_player_name" in draft.columns else "player_name"

        return [
            {
                "round": int(row["round"]),
                "pick": int(row["pick"]),
                "player": str(row.get(name_col, "")),
                "position": str(row.get("position", "")),
            }
            for _, row in team_oline.iterrows()
        ]

    # ------------------------------------------------------------------
    # Data pre-aggregation — all Python, zero API calls
    # ------------------------------------------------------------------

    async def _build_team_context(self, team: str) -> dict:
        """
        Pre-fetch and aggregate ALL data for one team.
        Returns a compact dict ready to pass to the model.
        No API calls here — reads from warehouse only.
        """
        current_season = get_current_season()
        analysis_seasons = get_analysis_seasons(3)

        # Use most recent season with available data for stats.
        stats_season = current_season
        if self._warehouse.get_seasonal_stats(current_season).empty:
            for s in sorted(analysis_seasons, reverse=True):
                if not self._warehouse.get_seasonal_stats(s).empty:
                    stats_season = s
                    break

        oline = await self._get_oline_data(team, stats_season)
        qb    = await self._get_qb_data(team, stats_season)
        pers  = await self._get_personnel_data(team, stats_season)
        roster = await self._get_roster_summary(team)

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
        analysis_year = get_analysis_year()
        oline_picks = self._get_oline_draft_context(team, analysis_year)

        oline_stats = self._warehouse.get_oline_stats(season)
        if oline_stats.empty:
            return {
                "team": team,
                "oline_draft_picks": oline_picks,
                "note": "No historical sack rate data — use model knowledge and oline_draft_picks.",
            }

        team_row = oline_stats[oline_stats["team"] == team]
        if team_row.empty:
            return {
                "team": team,
                "oline_draft_picks": oline_picks,
                "note": "No historical sack rate data — use model knowledge and oline_draft_picks.",
            }

        row = team_row.iloc[0]
        sack_rate = row.get("sack_rate")
        if sack_rate is not None and pd.notna(sack_rate):
            sack_rate = round(float(sack_rate), 4)
        else:
            sack_rate = None

        avg_ttt = row.get("avg_time_to_throw")
        if avg_ttt is not None and pd.notna(avg_ttt):
            avg_ttt = round(float(avg_ttt), 3)
        else:
            avg_ttt = None

        total_dropbacks = int(row.get("total_dropbacks", 0) or 0)

        return {
            "team": team,
            "season": season,
            "total_dropbacks": total_dropbacks,
            "sack_rate": sack_rate,
            "avg_time_to_throw": avg_ttt,
            "oline_draft_picks": oline_picks,
            "note": (
                "Use sack_rate and avg_time_to_throw as primary inputs for historical "
                "pass_protection_grade. "
                "IMPORTANT: Also factor in oline_draft_picks — draft capital invested in OLine "
                "is a strong forward-looking signal. "
                "A Round 1 OT pick should improve pass_protection_grade by 1-2 letter grades "
                "above the historical sack rate baseline. "
                "A Round 1 OG/C pick improves run_blocking_grade similarly. "
                "Multiple OLine picks = multiply the effect. "
                "No OLine picks + high sack rate = grade stays at historical level."
            ),
        }

    async def _get_qb_data(self, team: str, season: int) -> dict:
        """
        Three-source QB identification:
        0. Depth chart QB1 (most authoritative for current season)
        1. Seasonal roster (current_season) → who IS the QB
        2. QB stats from warehouse → pull that QB's stats
        3. Fallback: most passing yards on this team
        """
        current_season = get_current_season()

        # --- Source 0: Depth chart QB1 (most authoritative) ---
        starter_name = None
        if hasattr(self._warehouse, "get_starter"):
            dc_starter = self._warehouse.get_starter(team, "QB")
            if dc_starter:
                starter_name = dc_starter["name"]
                logger.debug("%s: QB1 from depth chart: %s", team, starter_name)

        # --- Source 1: Identify current QB from seasonal roster (fallback) ---
        roster = self._warehouse.seasonal_rosters
        if not starter_name and not roster.empty:
            team_qbs = roster[
                (roster["team"] == team)
                & (roster["position"] == "QB")
                & (roster["status"] == "ACT")
            ]
            if not team_qbs.empty:
                if len(team_qbs) == 1:
                    starter_name = team_qbs.iloc[0]["player_name"]
                else:
                    # Multiple active QBs — cross-reference with stats to find starter
                    qb_stats_df = self._warehouse.get_qb_stats(season)
                    if not qb_stats_df.empty:
                        team_col = "recent_team" if "recent_team" in qb_stats_df.columns else "team"
                        name_col_qs = "player_name" if "player_name" in qb_stats_df.columns else "player_display_name"
                        team_qb_stats = qb_stats_df[qb_stats_df[team_col] == team]
                        if not team_qb_stats.empty:
                            starter_name = team_qb_stats.sort_values(
                                "passing_yards", ascending=False, na_position="last"
                            ).iloc[0][name_col_qs]
                    if starter_name is None:
                        starter_name = team_qbs.iloc[0]["player_name"]

        # --- Sleeper years_exp for the identified QB ---
        qb_years_exp = None
        if starter_name:
            sleeper_rosters = self._warehouse.rosters
            if not sleeper_rosters.empty:
                name_col_r = "full_name" if "full_name" in sleeper_rosters.columns else "player_name"
                qb_match = sleeper_rosters[
                    (sleeper_rosters[name_col_r] == starter_name)
                    & (sleeper_rosters["position"] == "QB")
                ]
                if not qb_match.empty:
                    yrs = qb_match.iloc[0].get("years_exp")
                    if yrs is not None and pd.notna(yrs):
                        qb_years_exp = int(yrs)

        # --- Source 2: QB stats from warehouse ---
        qb_stats = self._warehouse.get_qb_stats(season)
        if qb_stats.empty:
            if starter_name:
                result = {
                    "team": team, "season": season,
                    "starter_name": starter_name,
                    "source": "roster_only",
                    "note": f"{starter_name} identified from roster but no stats available — use model knowledge",
                }
                if qb_years_exp is not None:
                    result["years_exp"] = qb_years_exp
                return result
            return {"team": team, "note": "No QB data — use model knowledge"}

        # Find the QB's stats
        name_col = "player_name" if "player_name" in qb_stats.columns else "player_display_name"

        if starter_name:
            norm_target = normalize_player_name(starter_name)
            qb_copy = qb_stats.copy()
            qb_copy["_norm"] = qb_copy[name_col].apply(normalize_player_name)
            starter_df = qb_copy[qb_copy["_norm"] == norm_target]

            # Log QB change if roster QB differs from stats leader on this team
            team_col = "recent_team" if "recent_team" in qb_stats.columns else "team"
            team_qb_stats = qb_stats[qb_stats[team_col] == team]
            if not team_qb_stats.empty:
                stats_leader = team_qb_stats.sort_values(
                    "passing_yards", ascending=False
                ).iloc[0][name_col]
                if normalize_player_name(stats_leader) != norm_target:
                    logger.info(
                        "QB CHANGE: %s — roster=%s, stats_leader=%s",
                        team, starter_name, stats_leader,
                    )

            if starter_df.empty:
                result = {
                    "team": team, "season": season,
                    "starter_name": starter_name,
                    "source": "roster_only",
                    "note": f"{starter_name} identified from roster but no stats found — use model knowledge",
                }
                if qb_years_exp is not None:
                    result["years_exp"] = qb_years_exp
                return result
            row = starter_df.iloc[0]
        else:
            # --- Fallback: most passing yards on this team ---
            team_col = "recent_team" if "recent_team" in qb_stats.columns else "team"
            team_qb_data = qb_stats[qb_stats[team_col] == team]
            if team_qb_data.empty:
                return {"team": team, "note": "No QB data — use model knowledge"}
            row = team_qb_data.sort_values("passing_yards", ascending=False).iloc[0]
            starter_name = row[name_col]

        def _safe_int(val: object) -> int:
            try:
                return int(val)
            except (TypeError, ValueError):
                return 0

        total_att = _safe_int(row.get("attempts", 0))
        completions = _safe_int(row.get("completions", 0))
        games = _safe_int(row.get("games", 0))

        result = {
            "team": team,
            "season": season,
            "starter_name": starter_name,
            "source": "roster+stats" if not roster.empty else "stats_fallback",
            "games_played": games,
            "total_attempts": total_att,
            "completion_pct": round(completions / total_att, 3) if total_att > 0 else None,
            "passing_yards": _safe_int(row.get("passing_yards", 0)),
            "passing_tds": _safe_int(row.get("passing_tds", 0)),
            "interceptions": _safe_int(row.get("interceptions", 0)),
            "rushing_yards": _safe_int(row.get("rushing_yards", 0)),
            "rushing_tds": _safe_int(row.get("rushing_tds", 0)),
            "note": "Supplement with your knowledge of this QB's performance under pressure and CPOE.",
        }
        if qb_years_exp is not None:
            result["years_exp"] = qb_years_exp
        return result

    async def _get_personnel_data(self, team: str, season: int) -> dict:
        ts_df = self._warehouse.get_target_share(season)
        if ts_df.empty:
            return {"team": team, "note": "No data — use model knowledge"}

        team_col = "recent_team" if "recent_team" in ts_df.columns else "team"
        skill = ts_df[
            (ts_df[team_col] == team) &
            (ts_df["position"].isin(["WR", "TE", "RB"]))
        ]
        if skill.empty:
            return {"team": team, "note": "No skill position data"}

        # Target distribution by position
        targets_col = "total_targets" if "total_targets" in skill.columns else "targets"
        pos_targets = skill.groupby("position")[targets_col].sum()
        total_targets = pos_targets.sum()
        target_share_by_pos = {
            str(pos): round(float(t / total_targets), 3)
            for pos, t in pos_targets.items()
        } if total_targets > 0 else {}

        # Top TD scorers
        td_col = "total_rec_tds" if "total_rec_tds" in skill.columns else "receiving_tds"
        name_col = "player_name" if "player_name" in skill.columns else "player_display_name"
        top_scorers = (
            skill[[name_col, "position", td_col]]
            .sort_values(td_col, ascending=False)
            .head(5)
            .rename(columns={name_col: "player_name", td_col: "receiving_tds"})
            .to_dict(orient="records")
        )

        return {
            "team": team,
            "season": season,
            "target_share_by_position": target_share_by_pos,
            "top_td_scorers": top_scorers,
            "note": "Supplement with your knowledge of 11/12/21 personnel rates and red zone tendencies.",
        }

    async def _get_roster_summary(self, team: str) -> dict:
        rosters = self._warehouse.rosters
        if rosters.empty:
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
        if self._warehouse is None:
            from backend.integrations.nfl_data import NflDataWarehouse
            self._warehouse = NflDataWarehouse.build()

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

            # Detect QB mismatch: if model analyzed a different QB than
            # the Sleeper depth chart starter, the entire analysis (qb_tier,
            # notes, system_grade) is calibrated for the wrong player.
            # Re-run with an explicit QB correction so all fields are consistent.
            qb_data = context.get("qb_metrics", {})
            data_qb = qb_data.get("starter_name")
            model_qb = data.get("qb_name")
            if data_qb and model_qb:
                data_norm = normalize_player_name(data_qb)
                model_norm = normalize_player_name(model_qb)
                if data_norm != model_norm:
                    logger.warning(
                        "%s: QB mismatch — model=%r, depth_chart=%r — re-running",
                        team, model_qb, data_qb,
                    )
                    # Add explicit QB correction to context and re-run
                    context["qb_override"] = (
                        f"IMPORTANT: The current starting QB for {team} is "
                        f"{data_qb} (from Sleeper depth chart, depth_chart_order=1). "
                        f"You MUST generate ALL analysis — qb_name, qb_tier, notes, "
                        f"system_grade — for {data_qb}, NOT {model_qb}. "
                        f"Do not speculate about future QB changes."
                    )
                    raw2 = await self.call_once(
                        system=SYSTEM_PROMPT,
                        user=json.dumps(context, default=str),
                        input_data=context,
                        entity_id=team,
                    )
                    if raw2:
                        data2 = parse_json_output(raw2)
                        if isinstance(data2, dict):
                            data = data2
                            data["team_abbr"] = team
                    # Final enforcement: even after re-run, pin the name
                    data["qb_name"] = data_qb

            # Enforce rookie_qb_flag from Sleeper years_exp when available.
            # Model may flag a veteran as rookie (e.g. predicting a different
            # starter than depth chart shows).
            qb_years = qb_data.get("years_exp")
            if qb_years is not None and qb_years >= 3 and data.get("rookie_qb_flag"):
                logger.info(
                    "%s: overriding rookie_qb_flag to False — %s has %d years NFL exp",
                    team, data.get("qb_name"), qb_years,
                )
                data["rookie_qb_flag"] = False
                data["compound_risk_flag"] = False

            # Attach Python-computed numerics (NOT from model output)
            oline_data = context.get("oline", {})
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

    async def run_all_teams(
        self, warehouse=None, concurrency: int = 10
    ) -> dict[str, bool]:
        """
        Run for all 32 teams with bounded concurrency.
        Reads all data from the warehouse — no independent data fetching.
        Returns {team_abbr: success_bool}.
        """
        if warehouse is not None:
            self._warehouse = warehouse
        if self._warehouse is None:
            from backend.integrations.nfl_data import NflDataWarehouse
            self._warehouse = NflDataWarehouse.build()

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


async def run_all_teams(
    concurrency: int = 10, dry_run: bool = False, warehouse=None
) -> dict[str, bool]:
    return await _get_agent(dry_run).run_all_teams(warehouse=warehouse, concurrency=concurrency)
