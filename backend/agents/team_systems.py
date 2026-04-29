"""
Agent 1: Team Systems Agent

Grades every NFL team as an offensive system. Runs first in the pre-draft pipeline.
Output is inherited by all other agents.

For each of the 32 NFL teams, the agent:
1. Pulls O-line grades, QB metrics, OC history, and personnel tendencies via tools
2. Reasons through system quality and risk flags
3. Writes a structured TeamSystem record to the database

Key flags produced:
  - rookie_qb_flag: true for any first-year starter
  - compound_risk_flag: rookie QB AND pass protection grade C or below
    → cascades as a severe penalty to all skill positions on that roster
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import (
    run_agent, tool, string_prop, number_prop, bool_prop, array_prop, extract_json
)
from backend.database import AsyncSessionLocal
from backend.integrations import nfl_data, overthecap
from backend.models.team_system import TeamSystem

logger = logging.getLogger(__name__)

CURRENT_SEASON = 2024

# ---------------------------------------------------------------------------
# All 32 NFL teams
# ---------------------------------------------------------------------------

NFL_TEAMS = [
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB",  "HOU", "IND", "JAX", "KC",
    "LA",  "LAC", "LV",  "MIA", "MIN", "NE",  "NO",  "NYG",
    "NYJ", "PHI", "PIT", "SEA", "SF",  "TB",  "TEN", "WAS",
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an NFL offensive system analyst building a pre-draft fantasy football research database.

Your job is to grade every NFL team's offensive system for the upcoming season. You have tools to retrieve:
- O-line pass protection and run blocking grades
- QB metrics (CPOE, air yards, pressure performance)
- Offensive coordinator history and scheme tendencies
- Personnel grouping tendencies and red zone philosophy

After gathering data, produce a JSON object matching this exact schema:
{
  "team_abbr": "string (3-5 char NFL team code)",
  "pass_protection_grade": "string (A+/A/A-/B+/B/B-/C+/C/C-/D+/D/F)",
  "run_blocking_grade": "string",
  "qb_name": "string",
  "qb_tier": "string (elite/solid/average/weak/rookie)",
  "qb_experience_years": integer,
  "qb_pressure_performance": "string (elite/above_avg/avg/below_avg)",
  "qb_cpoe": number (completion pct over expectation, e.g. 2.4),
  "qb_air_yards_per_attempt": number,
  "qb_downfield_aggressiveness": "string (aggressive/moderate/conservative)",
  "rookie_qb_flag": boolean,
  "compound_risk_flag": boolean,
  "oc_name": "string",
  "oc_scheme": "string (balanced/pass_heavy/run_heavy/west_coast/air_raid/spread)",
  "oc_run_pass_split_tendency": number (0.0-1.0, pass rate),
  "personnel_tendency": "string (11/12/21/22/13)",
  "red_zone_philosophy": "string (wr1/te/rb/spread/qb_scramble)",
  "system_ceiling": "string (high/moderate/low)",
  "system_grade": "string (A+/A/A-/B+/B/B-/C+/C/C-/D+/D/F)",
  "notes": "string (2-3 sentences synthesizing the system for a fantasy analyst)"
}

Rules:
- rookie_qb_flag = true if this is the QB's first full season as a starter
- compound_risk_flag = true ONLY when rookie_qb_flag is true AND pass_protection_grade is C or below
- compound_risk_flag cascades severe penalties to all skill position players — flag conservatively
- Use your knowledge of the 2024 NFL season and known 2025 offseason changes
- The notes field should focus on fantasy implications, not just general football analysis
- Output ONLY the JSON object, no other text
"""

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    tool(
        "get_oline_grades",
        "Get offensive line pass protection and run blocking performance metrics for a team. "
        "Returns recent season grades derived from nfl_data_py pressure rates and sack data.",
        {
            "team_abbr": string_prop("NFL team abbreviation (e.g. 'KC', 'LAC', 'SF')"),
        },
        required=["team_abbr"],
    ),
    tool(
        "get_qb_metrics",
        "Get QB performance metrics for the starting QB of a team: CPOE, air yards per attempt, "
        "pressure rate faced, performance under pressure, passing touchdowns and interceptions.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
        },
        required=["team_abbr"],
    ),
    tool(
        "get_oc_history",
        "Get offensive coordinator identity and historical scheme tendencies. "
        "Returns OC name, current scheme, run/pass split from last 3 seasons at all stops.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
        },
        required=["team_abbr"],
    ),
    tool(
        "get_personnel_tendencies",
        "Get personnel grouping tendencies (11/12/21 personnel rates) and red zone target distribution.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
        },
        required=["team_abbr"],
    ),
    tool(
        "get_team_roster",
        "Get current roster for a team showing QB, key skill players, and O-line starters.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
        },
        required=["team_abbr"],
    ),
]

# ---------------------------------------------------------------------------
# Tool handlers — pull real data from nfl_data_py, supplement with model knowledge
# ---------------------------------------------------------------------------

async def handle_tool(name: str, inputs: dict) -> dict:
    team = inputs.get("team_abbr", "").upper()

    if name == "get_oline_grades":
        return await _get_oline_grades(team)

    elif name == "get_qb_metrics":
        return await _get_qb_metrics(team)

    elif name == "get_oc_history":
        # OC history requires model knowledge — return structured prompt for model to fill
        return {
            "note": "Use your knowledge of this team's current OC, their scheme history, "
                    "and run/pass tendencies from their last 3 coaching stops.",
            "team": team,
        }

    elif name == "get_personnel_tendencies":
        return await _get_personnel_tendencies(team)

    elif name == "get_team_roster":
        return await _get_roster(team)

    return {"error": f"Unknown tool: {name}"}


async def _get_oline_grades(team: str) -> dict:
    try:
        pbp_like = nfl_data.fetch_weekly_stats(CURRENT_SEASON)
        team_df = pbp_like[pbp_like["recent_team"] == team]

        # Use sack rate and pressure-related proxies
        # sack_yards as a proxy (negative impact on offense → worse OL)
        if team_df.empty:
            return {"team": team, "note": "No data found — use model knowledge"}

        total_attempts = team_df["attempts"].sum()
        total_sack_yards = team_df["sack_yards"].sum() if "sack_yards" in team_df else 0
        total_sacks = team_df["sacks"].sum() if "sacks" in team_df else 0
        sack_rate = float(total_sacks / total_attempts) if total_attempts > 0 else None

        return {
            "team": team,
            "season": CURRENT_SEASON,
            "total_dropbacks": int(total_attempts),
            "sack_rate": round(sack_rate, 4) if sack_rate else None,
            "note": (
                "Sack rate is a proxy for pass protection. Supplement with your knowledge "
                "of this team's O-line personnel and PFF-style grades."
            ),
        }
    except Exception as exc:
        logger.warning("OLine grades error for %s: %s", team, exc)
        return {"team": team, "error": str(exc)}


async def _get_qb_metrics(team: str) -> dict:
    try:
        weekly = nfl_data.fetch_weekly_stats(CURRENT_SEASON)
        # Find QB for this team (most attempts)
        qb_data = weekly[
            (weekly["recent_team"] == team) & (weekly["position"] == "QB")
        ]
        if qb_data.empty:
            return {"team": team, "note": "No QB data — use model knowledge"}

        starter = (
            qb_data.groupby("player_name")["attempts"].sum()
            .sort_values(ascending=False)
            .reset_index()
        )
        starter_name = starter.iloc[0]["player_name"] if len(starter) > 0 else "Unknown"

        starter_df = qb_data[qb_data["player_name"] == starter_name]
        games = len(starter_df)

        metrics = {
            "team": team,
            "season": CURRENT_SEASON,
            "starter_name": starter_name,
            "games_played": int(games),
            "total_attempts": int(starter_df["attempts"].sum()),
            "total_completions": int(starter_df["completions"].sum()),
            "completion_pct": round(float(starter_df["completions"].sum() / starter_df["attempts"].sum()), 3)
                              if starter_df["attempts"].sum() > 0 else None,
            "passing_yards": int(starter_df["passing_yards"].sum()),
            "passing_tds": int(starter_df["passing_tds"].sum()),
            "interceptions": int(starter_df["interceptions"].sum()),
            "passing_air_yards": int(starter_df["passing_air_yards"].sum()),
            "air_yards_per_attempt": round(float(starter_df["passing_air_yards"].sum() /
                                                 starter_df["attempts"].sum()), 2)
                                     if starter_df["attempts"].sum() > 0 else None,
            "dakota": round(float(starter_df["dakota"].mean()), 3)
                      if "dakota" in starter_df.columns and not starter_df["dakota"].isna().all()
                      else None,
            "note": "Supplement sack-adjusted CPOE with your knowledge of this QB's performance under pressure.",
        }
        return metrics
    except Exception as exc:
        logger.warning("QB metrics error for %s: %s", team, exc)
        return {"team": team, "error": str(exc)}


async def _get_personnel_tendencies(team: str) -> dict:
    try:
        weekly = nfl_data.fetch_weekly_stats(CURRENT_SEASON)
        team_skill = weekly[
            (weekly["recent_team"] == team) &
            (weekly["position"].isin(["WR", "TE", "RB"]))
        ]

        if team_skill.empty:
            return {"team": team, "note": "No weekly data — use model knowledge"}

        # Target share by position as proxy for personnel grouping
        pos_targets = (
            team_skill.groupby("position")["targets"].sum()
            .sort_values(ascending=False)
        )
        total_targets = pos_targets.sum()
        pos_target_share = {
            str(pos): round(float(tgts / total_targets), 3)
            for pos, tgts in pos_targets.items()
        } if total_targets > 0 else {}

        # Top target receivers to infer red zone usage
        top_receivers = (
            team_skill.groupby(["player_name", "position"])["receiving_tds"]
            .sum()
            .sort_values(ascending=False)
            .head(5)
            .reset_index()
            .to_dict(orient="records")
        )

        return {
            "team": team,
            "season": CURRENT_SEASON,
            "target_share_by_position": pos_target_share,
            "top_td_scorers": top_receivers,
            "note": (
                "Use your knowledge of this team's 11/12/21 personnel grouping rates "
                "and specific red zone target tendencies to supplement this data."
            ),
        }
    except Exception as exc:
        logger.warning("Personnel tendencies error for %s: %s", team, exc)
        return {"team": team, "error": str(exc)}


async def _get_roster(team: str) -> dict:
    try:
        rosters = nfl_data.fetch_rosters(CURRENT_SEASON)
        team_roster = rosters[rosters["team"] == team][
            ["player_name", "position", "depth_chart_position", "status"]
        ].head(25)

        skill = team_roster[team_roster["position"].isin(["QB", "RB", "WR", "TE", "OL", "T", "G", "C"])]
        return {
            "team": team,
            "roster": skill.to_dict(orient="records"),
        }
    except Exception as exc:
        logger.warning("Roster error for %s: %s", team, exc)
        return {"team": team, "error": str(exc)}


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

async def _upsert_team_system(session: AsyncSession, data: dict) -> None:
    team = data.get("team_abbr", "").upper()

    result = await session.execute(
        select(TeamSystem).where(
            TeamSystem.team_abbr == team,
            TeamSystem.season_year == CURRENT_SEASON,
        )
    )
    existing = result.scalar_one_or_none()

    def _grade(key: str) -> str | None:
        return data.get(key)

    if existing:
        record = existing
    else:
        record = TeamSystem(team_abbr=team, season_year=CURRENT_SEASON)
        session.add(record)

    record.pass_protection_grade      = _grade("pass_protection_grade")
    record.run_blocking_grade         = _grade("run_blocking_grade")
    record.qb_name                    = data.get("qb_name")
    record.qb_tier                    = data.get("qb_tier")
    record.qb_experience_years        = data.get("qb_experience_years")
    record.qb_pressure_performance    = data.get("qb_pressure_performance")
    record.qb_cpoe                    = data.get("qb_cpoe")
    record.qb_air_yards_per_attempt   = data.get("qb_air_yards_per_attempt")
    record.qb_downfield_aggressiveness = data.get("qb_downfield_aggressiveness")
    record.rookie_qb_flag             = bool(data.get("rookie_qb_flag", False))
    record.compound_risk_flag         = bool(data.get("compound_risk_flag", False))
    record.oc_name                    = data.get("oc_name")
    record.oc_scheme                  = data.get("oc_scheme")
    record.oc_run_pass_split_tendency = data.get("oc_run_pass_split_tendency")
    record.personnel_tendency         = data.get("personnel_tendency")
    record.red_zone_philosophy        = data.get("red_zone_philosophy")
    record.system_ceiling             = data.get("system_ceiling")
    record.system_grade               = data.get("system_grade")
    record.notes                      = data.get("notes")

    await session.commit()
    logger.info("Upserted TeamSystem: %s — Grade: %s  rookie_qb=%s  compound_risk=%s",
                team, record.system_grade, record.rookie_qb_flag, record.compound_risk_flag)


# ---------------------------------------------------------------------------
# Per-team runner
# ---------------------------------------------------------------------------

async def run_for_team(team_abbr: str) -> dict | None:
    """Run the agent for one team and write to DB. Returns the parsed JSON or None on failure."""
    logger.info("Running Team Systems Agent for %s", team_abbr)

    user_message = (
        f"Analyze the {team_abbr} offensive system for the 2025 NFL season. "
        f"Use your tools to gather data, then produce the JSON grade record. "
        f"Account for any known 2025 offseason changes (new OC, QB change, key O-line additions/losses)."
    )

    try:
        raw = await run_agent(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tools=TOOLS,
            tool_handler=handle_tool,
            max_tokens=4096,
            temperature=0.1,  # Low temp for structured factual output
        )

        data = extract_json(raw)
        if not data or not isinstance(data, dict):
            logger.error("Failed to extract JSON for %s. Raw output: %s", team_abbr, raw[:500])
            return None

        # Enforce team_abbr from our list (model sometimes uses wrong abbr)
        data["team_abbr"] = team_abbr

        async with AsyncSessionLocal() as session:
            await _upsert_team_system(session, data)

        return data

    except Exception as exc:
        logger.error("Team Systems Agent failed for %s: %s", team_abbr, exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Full pipeline runner
# ---------------------------------------------------------------------------

async def run_all_teams(concurrency: int = 4) -> dict[str, bool]:
    """
    Run Team Systems Agent for all 32 NFL teams with bounded concurrency.
    Returns {team_abbr: success_bool}.
    """
    logger.info("Starting Team Systems pipeline for all 32 teams (concurrency=%d)", concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, bool] = {}

    async def _run_one(team: str):
        async with semaphore:
            data = await run_for_team(team)
            results[team] = data is not None

    import asyncio as _asyncio
    await _asyncio.gather(*[_run_one(t) for t in NFL_TEAMS])

    success = sum(1 for v in results.values() if v)
    logger.info("Team Systems pipeline complete: %d/32 teams successful", success)
    return results


import asyncio  # noqa: E402
