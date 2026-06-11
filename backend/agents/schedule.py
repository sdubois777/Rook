"""
Agent 5: Schedule Agent

Grades every player's schedule across three windows:
  - Early (weeks 1-6): fast-start relevance
  - Full season: overall schedule quality
  - Playoff (weeks 14-17): first-class field, most underrated metric

Architecture:
  - Model: Haiku (data extraction and classification)
  - Max tokens: 800 per team batch
  - Pattern: pre-aggregate in Python → ONE call_once() per team → parse JSON → write DB
  - Never uses run_agent() (that is for live draft only)

Defensive grades are computed from the most recently completed season's weekly data.
The model outputs ONE JSON object per team with position-group grades (WR / RB / TE).
Python then writes per-player records from those position-group grades.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU
from backend.agents.team_systems import NFL_TEAMS
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerSchedule
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

logger = logging.getLogger(__name__)

SKILL_POSITIONS = {"WR", "RB", "TE", "QB"}

# Outdoor cold-weather cities — weather risk modifier for late-season games (weeks 10+)
WEATHER_RISK_TEAMS = {"BUF", "GB", "CHI", "NE", "CLE", "PIT"}

# Schedule windows
EARLY_WINDOW   = set(range(1, 7))     # weeks 1-6
PLAYOFF_WINDOW = set(range(14, 18))   # weeks 14-17

# Defensive grade thresholds (rank out of 32 teams, by PPR allowed per game)
# Rank 1 = most points allowed = best offensive matchup = "favorable"
FAVORABLE_RANK_CUTOFF = 10
TOUGH_RANK_CUTOFF     = 23

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fantasy football schedule analyst building a pre-draft research database.

You receive pre-aggregated schedule data for one NFL team: the weekly opponent,
defensive grade per position group (favorable/neutral/tough), weather risk, and divisional flags.
All pattern detection (weather, divisional weeks) is pre-computed in Python.

Output a SINGLE JSON object (not an array) with this structure:
{
  "bye_week": integer,
  "WR": {
    "early_window_grade": "favorable|neutral|tough",
    "early_window_favorable_weeks": [int, ...],
    "early_window_tough_weeks": [int, ...],
    "early_window_summary": "1 sentence",
    "full_season_grade": "favorable|neutral|tough",
    "playoff_window_grade": "favorable|neutral|tough",
    "playoff_weeks": [14, 15, 16, 17],
    "playoff_matchups": [{"week": int, "opponent": "str", "grade": "favorable|neutral|tough"}],
    "playoff_summary": "1 sentence",
    "weather_risk": "low|moderate|high",
    "weather_affected_weeks": [int, ...],
    "divisional_game_weeks": [int, ...],
    "schedule_score": float (1.0-10.0),
    "schedule_notes": "1-2 sentences"
  },
  "RB": { ... same schema ... },
  "TE": { ... same schema ... }
}

Window grading rules:
  - early_window_grade: majority of weeks 1-6 defensive grades (favorable if 3+ favorable)
  - playoff_window_grade: average of weeks 14-17 grades (MOST IMPORTANT — weight heavily in schedule_score)
  - full_season_grade: overall quality across all 18 weeks

schedule_score (1.0-10.0):
  - Weighting: playoff_window (50%) > early_window (30%) > full_season (20%)
  - favorable window = high sub-score; tough window = low sub-score
  - 8-10: excellent (favorable playoff + early); 4-6: average; 1-3: tough playoff window

playoff_window_grade is a first-class output field — never omit it or bury it in notes.

weather_risk:
  - "low": no outdoor cold-weather games in weeks 10+ (weather_affected_weeks is empty)
  - "moderate": 1-2 such games
  - "high": 3+ such games

Output ONLY valid JSON. No explanation, no preamble, no markdown fences."""


# ---------------------------------------------------------------------------
# Defensive grade helpers — pure functions, fully testable
# ---------------------------------------------------------------------------

def compute_def_grades(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute position-group defensive grades for each team from weekly player stats.

    Returns DataFrame with columns:
      defense_team, position, ppr_per_game, rank, grade

    Rank 1 = most PPR allowed per game = weakest defense = "favorable" matchup.
    """
    if weekly_df.empty:
        return pd.DataFrame(
            columns=["defense_team", "position", "ppr_per_game", "rank", "grade"]
        )

    # Filter to regular season skill positions
    reg = weekly_df[
        weekly_df["season_type"] == "REG"
    ] if "season_type" in weekly_df.columns else weekly_df.copy()

    skill = reg[reg["position"].isin({"WR", "RB", "TE"})].copy()
    if skill.empty:
        return pd.DataFrame(
            columns=["defense_team", "position", "ppr_per_game", "rank", "grade"]
        )

    # Sum PPR per defensive team per position per week (= per game)
    game_stats = (
        skill.groupby(["opponent_team", "position", "week"])["fantasy_points_ppr"]
        .sum()
        .reset_index()
    )

    # Average across all games
    def_avg = (
        game_stats.groupby(["opponent_team", "position"])
        .agg(ppr_per_game=("fantasy_points_ppr", "mean"))
        .reset_index()
        .rename(columns={"opponent_team": "defense_team"})
    )

    # Rank within each position group (1 = most allowed = best for offense)
    def_avg["rank"] = (
        def_avg.groupby("position")["ppr_per_game"]
        .rank(ascending=False, method="first")
        .astype(int)
    )

    def _grade(rank: int) -> str:
        if rank <= FAVORABLE_RANK_CUTOFF:
            return "favorable"
        if rank <= TOUGH_RANK_CUTOFF:
            return "neutral"
        return "tough"

    def_avg["grade"] = def_avg["rank"].apply(_grade)
    return def_avg


def is_weather_risk_game(
    home_team: str,
    week: int,
    roof: str | None = None,
) -> bool:
    """
    Return True if the game carries weather risk for passing offenses.

    Criteria: home team is in an outdoor cold-weather city AND week >= 10.
    Optionally uses the `roof` column from schedule data for precision.
    """
    if week < 10:
        return False
    if roof is not None and roof not in ("outdoors", "open"):
        return False
    return home_team in WEATHER_RISK_TEAMS


def lookup_def_grade(
    def_grades_df: pd.DataFrame,
    opponent: str,
    position: str,
) -> str:
    """Look up the defensive grade for opponent vs a given position. Defaults to neutral."""
    if def_grades_df.empty:
        return "neutral"
    mask = (
        (def_grades_df["defense_team"] == opponent) &
        (def_grades_df["position"] == position)
    )
    rows = def_grades_df[mask]
    if rows.empty:
        return "neutral"
    return str(rows.iloc[0]["grade"])


# ---------------------------------------------------------------------------
# ScheduleAgent
# ---------------------------------------------------------------------------

class ScheduleAgent(BaseAgent):
    AGENT_NAME       = "schedule"
    AGENT_MODEL      = HAIKU
    AGENT_MAX_TOKENS = 1500

    # ------------------------------------------------------------------
    # Sync data helpers — read from warehouse (no network calls)
    # ------------------------------------------------------------------

    def _get_team_roster(self, team: str, season: int) -> list[dict]:
        """Return skill-position players for this team from warehouse roster data."""
        rosters = self._warehouse.rosters
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
            result.append({"name": name, "position": pos})

        return result

    def _get_team_schedule_weeks(self, team: str) -> list[dict]:
        """
        Build per-week schedule context for a team, including:
          - opponent, is_home, divisional, weather_risk
          - def_vs_wr / def_vs_rb / def_vs_te grades (from pre-computed def_grades)
        Returns list sorted by week, excluding the bye week.
        """
        sched_df   = self._warehouse.schedule
        def_grades = self._warehouse.get_most_recent_def_grades()

        if sched_df is None or sched_df.empty:
            return []

        # Filter to regular season games involving this team
        mask = (
            ((sched_df["home_team"] == team) | (sched_df["away_team"] == team)) &
            (sched_df["game_type"] == "REG")
        ) if "game_type" in sched_df.columns else (
            (sched_df["home_team"] == team) | (sched_df["away_team"] == team)
        )
        team_df = sched_df[mask].copy()

        weeks: list[dict] = []
        for _, row in team_df.iterrows():
            home_team = str(row["home_team"])
            away_team = str(row["away_team"])
            is_home   = home_team == team
            opponent  = away_team if is_home else home_team
            week      = int(row["week"])
            is_div    = bool(row.get("div_game", 0))
            roof      = str(row.get("roof", "")) or None
            wx_risk   = is_weather_risk_game(home_team, week, roof)

            weeks.append({
                "week":       week,
                "opponent":   opponent,
                "is_home":    is_home,
                "divisional": is_div,
                "weather_risk": wx_risk,
                "def_vs_wr":  lookup_def_grade(def_grades, opponent, "WR"),
                "def_vs_rb":  lookup_def_grade(def_grades, opponent, "RB"),
                "def_vs_te":  lookup_def_grade(def_grades, opponent, "TE"),
            })

        return sorted(weeks, key=lambda w: w["week"])

    def _get_bye_week(self, team: str) -> int | None:
        """Compute the bye week by finding the missing week in the team's schedule."""
        sched_df = self._warehouse.schedule
        if sched_df is None or sched_df.empty:
            return None

        mask = (sched_df["home_team"] == team) | (sched_df["away_team"] == team)
        if "game_type" in sched_df.columns:
            mask = mask & (sched_df["game_type"] == "REG")

        team_weeks = set(sched_df[mask]["week"].astype(int).tolist())
        all_weeks  = set(range(1, 19))
        byes       = all_weeks - team_weeks
        return int(min(byes)) if byes else None

    # ------------------------------------------------------------------
    # Context builder — all Python, zero API calls
    # ------------------------------------------------------------------

    async def _build_team_context(self, team_abbr: str) -> dict:
        team           = team_abbr.upper()
        current_season = get_current_season()
        analysis_year  = get_analysis_year()

        roster         = self._get_team_roster(team, current_season)
        schedule_weeks = self._get_team_schedule_weeks(team)
        bye_week       = self._get_bye_week(team)
        schedule_year  = self._warehouse.schedule_year

        # Retrieve team system context (coordinator scheme for adjustment context)
        team_system = await self._get_team_system(team)

        # Split schedule into windows
        early_weeks   = [w for w in schedule_weeks if w["week"] in EARLY_WINDOW]
        playoff_weeks = [w for w in schedule_weeks if w["week"] in PLAYOFF_WINDOW]

        return {
            "team":          team,
            "analysis_year": analysis_year,
            "schedule_year": schedule_year,
            "bye_week":      bye_week,
            "team_system":   team_system,
            "players":       [{"name": p["name"], "position": p["position"]} for p in roster],
            "schedule": {
                "early_window":   early_weeks,
                "full_season":    schedule_weeks,
                "playoff_window": playoff_weeks,
            },
        }

    async def _get_team_system(self, team: str) -> dict:
        """Pull team system context for OC scheme / coordinator adjustment hints."""
        from backend.models.team_system import TeamSystem
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(TeamSystem).where(TeamSystem.team_abbr == team)
            )
            ts = result.scalar_one_or_none()
            if not ts:
                return {}
            return {
                "qb_tier":    ts.qb_tier,
                "oc_scheme":  ts.oc_scheme,
                "system_grade": ts.system_grade,
            }

    # ------------------------------------------------------------------
    # Per-team runner — exactly ONE call_once()
    # ------------------------------------------------------------------

    async def run_for_team(self, team_abbr: str) -> int:
        """Run for one team. Returns number of schedule records written."""
        if self._warehouse is None:
            from backend.integrations.nfl_data import NflDataWarehouse
            self._warehouse = NflDataWarehouse.build()

        team = team_abbr.upper()
        logger.info("Building schedule context for %s", team)

        try:
            context = await self._build_team_context(team)

            if not context["players"] or not context["schedule"]["full_season"]:
                logger.info("%s: no players or no schedule data, skipping", team)
                return 0

            raw = await self.call_once(
                system=SYSTEM_PROMPT,
                user=(
                    f"Grade the {team} schedule for the {context['analysis_year']} season "
                    f"(schedule data from {context['schedule_year']}) "
                    f"by position group using this pre-aggregated data:\n\n"
                    f"{json.dumps(context, default=str)}"
                ),
                input_data=context,
                entity_id=team,
            )

            if not raw:
                return 0  # dry_run

            result = parse_json_output(raw)
            if not isinstance(result, dict):
                logger.error("%s: expected dict output, got %s", team, type(result))
                return 0

            written = await _write_schedules(result, context, team)
            logger.info("%s: %d schedule records written", team, written)
            return written

        except Exception as exc:
            logger.error("Schedule Agent failed for %s: %s", team, exc, exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Full pipeline — pre-warm caches once, then run all 32 teams
    # ------------------------------------------------------------------

    async def run_all_teams(
        self, warehouse=None, concurrency: int = 10
    ) -> dict[str, int]:
        """
        Reads all data from the warehouse — no independent data fetching.
        Returns {team_abbr: records_written}.
        """
        if warehouse is not None:
            self._warehouse = warehouse
        if self._warehouse is None:
            from backend.integrations.nfl_data import NflDataWarehouse
            self._warehouse = NflDataWarehouse.build()

        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, int] = {}

        async def _run_one(team: str) -> None:
            async with semaphore:
                results[team] = await self.run_for_team(team)

        await asyncio.gather(*[_run_one(t) for t in NFL_TEAMS])

        total = sum(results.values())
        logger.info("Schedule pipeline complete: %d total records written", total)
        return results


# ---------------------------------------------------------------------------
# Bulk DB write helpers
# ---------------------------------------------------------------------------

async def _bulk_resolve_player_ids(
    session: AsyncSession,
    names_and_teams: list[tuple[str, str]],
) -> dict[tuple[str, str], str | None]:
    """Resolve player IDs from (name, team) pairs in a single query."""
    results: dict[tuple, str | None] = {}
    unique_lasts = {n.split()[-1] for n, _ in names_and_teams if n}
    if not unique_lasts:
        return results

    conditions  = [Player.name.ilike(f"%{last}%") for last in unique_lasts]
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
        last       = name.split()[-1].lower()
        candidates = player_map.get(last, [])
        if not candidates:
            results[(name, team)] = None
        elif len(candidates) == 1:
            results[(name, team)] = str(candidates[0].id)
        else:
            match = [p for p in candidates if p.team_abbr and p.team_abbr.upper() == team.upper()]
            results[(name, team)] = str(match[0].id) if match else str(candidates[0].id)

    return results


async def _write_schedules(result: dict, context: dict, team: str) -> int:
    """
    Write PlayerSchedule records for each player on the team.

    `result` is the model output: {"bye_week": N, "WR": {...}, "RB": {...}, "TE": {...}}
    All players of a given position share the same schedule grades.
    QBs use WR grades (passing offense vs. pass defense).
    """
    if not result or not isinstance(result, dict):
        return 0

    players      = context.get("players", [])
    analysis_year = get_analysis_year()
    bye_week     = result.get("bye_week") or context.get("bye_week")

    # Position-group grades: QB and WR both use WR grades
    pos_grades: dict[str, dict] = {
        "WR": result.get("WR", {}),
        "RB": result.get("RB", {}),
        "TE": result.get("TE", {}),
        "QB": result.get("WR", {}),  # QBs use pass-defense grades
    }

    async with AsyncSessionLocal() as session:
        names_and_teams = [(p["name"], team) for p in players]
        id_map = await _bulk_resolve_player_ids(session, names_and_teams)

        written       = 0
        processed_ids: set[str] = set()   # guard against duplicate player_id in roster
        for player_info in players:
            pname     = player_info["name"]
            pos       = player_info.get("position", "WR")
            player_id = id_map.get((pname, team))
            if not player_id:
                logger.debug("Could not resolve player: %s (%s)", pname, team)
                continue

            if player_id in processed_ids:
                logger.debug("Skipping duplicate player_id for %s (%s)", pname, team)
                continue
            processed_ids.add(player_id)

            grades = pos_grades.get(pos, pos_grades.get("WR", {}))
            if not grades:
                continue

            # Upsert PlayerSchedule — use scalars().first() in case stale duplicates exist
            existing = (await session.execute(
                select(PlayerSchedule).where(
                    PlayerSchedule.player_id == player_id,
                    PlayerSchedule.season_year == analysis_year,
                )
            )).scalars().first()

            if existing:
                record = existing
            else:
                record = PlayerSchedule(player_id=player_id, season_year=analysis_year)
                session.add(record)

            record.bye_week                    = bye_week
            record.bye_in_playoff_window       = bool(bye_week and int(bye_week) in PLAYOFF_WINDOW)
            record.early_window_grade          = grades.get("early_window_grade")
            record.early_window_favorable_weeks = grades.get("early_window_favorable_weeks") or []
            record.early_window_tough_weeks    = grades.get("early_window_tough_weeks") or []
            record.early_window_summary        = grades.get("early_window_summary")
            record.full_season_grade           = grades.get("full_season_grade")
            record.playoff_window_grade        = grades.get("playoff_window_grade")
            record.playoff_weeks               = grades.get("playoff_weeks") or []
            record.playoff_matchups            = grades.get("playoff_matchups") or []
            record.playoff_summary             = grades.get("playoff_summary")
            record.weather_risk                = grades.get("weather_risk")
            record.weather_affected_weeks      = grades.get("weather_affected_weeks") or []
            record.divisional_game_weeks       = grades.get("divisional_game_weeks") or []
            record.schedule_score              = _to_decimal(grades.get("schedule_score"))
            record.schedule_notes              = grades.get("schedule_notes")

            written += 1

        await session.commit()

    return written


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 2)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Module-level compatibility shims
# ---------------------------------------------------------------------------

_agent_instance: ScheduleAgent | None = None


def _get_agent(dry_run: bool = False) -> ScheduleAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = ScheduleAgent(dry_run=dry_run)
    return _agent_instance


async def run_for_team(team_abbr: str, dry_run: bool = False) -> int:
    return await _get_agent(dry_run).run_for_team(team_abbr)


async def run_all_teams(
    concurrency: int = 10, dry_run: bool = False, warehouse=None
) -> dict[str, int]:
    return await _get_agent(dry_run).run_all_teams(warehouse=warehouse, concurrency=concurrency)
