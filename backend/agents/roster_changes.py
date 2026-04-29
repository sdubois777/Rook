"""
Agent 2: Roster Changes Agent

The most intellectually complex agent. Tracks every meaningful offseason transaction
and reasons through downstream consequences on player values.

Canonical test case (McConkey/Allen):
  Event: Keenan Allen signs with LAC
  → Allen's historical role: slot receiver, 27% target share with Herbert
  → McConkey's role: slot receiver, same alignment
  → Conclusion: direct role overlap → DISPLACED flag on McConkey
  → CONTINGENT/BENEFICIARY: McConkey value rises significantly if Allen misses time

Produces PlayerDependency records with flag types:
  DISPLACED, CONTINGENT, BENEFICIARY, COMMITTEE, SCHEME_FIT, COLLEGE_TRUST
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import (
    run_agent, tool, string_prop, number_prop, bool_prop, array_prop, extract_json
)
from backend.database import AsyncSessionLocal
from backend.integrations import nfl_data, overthecap
from backend.models.dependency import PlayerDependency
from backend.models.player import Player
from backend.models.team_system import TeamSystem

logger = logging.getLogger(__name__)

CURRENT_SEASON = 2024
ANALYSIS_YEAR = 2025  # The upcoming season we're building the draft bible for

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fantasy football analyst specializing in offseason transaction analysis.
Your job is to identify how roster changes affect player values through chain-of-reasoning.

You have tools to look up:
- Historical target share data for any player
- Current roster composition for any team
- Recent transactions (signings, cuts, trades)
- Team system grades (already analyzed)

For each meaningful transaction or situation, produce dependency flags in this JSON array format:
[
  {
    "player_name": "string (the player being flagged)",
    "player_team": "string (their current team abbr)",
    "player_position": "string (QB/RB/WR/TE)",
    "flag_type": "string (displaced|contingent|beneficiary|committee|scheme_fit|college_trust)",
    "trigger_player_name": "string (who caused this flag)",
    "trigger_player_team": "string",
    "trigger_condition": "string (active_and_healthy|injured|absent|traded)",
    "effect_on_value": "string (negative|positive|neutral)",
    "value_impact_pct": number (-1.0 to 1.0, e.g. -0.35 means 35% value reduction),
    "confidence": "string (high|medium|low)",
    "reasoning": "string (specific chain-of-reasoning, cite historical target share numbers)",
    "season_year": 2025
  }
]

Flag type definitions:
- displaced: Role directly overlapped by new arrival (negative impact when trigger player is healthy)
- contingent: Player's value is contingent on trigger player's health (value rises when trigger is out)
- beneficiary: Clear value increase when trigger player is absent
- committee: RB sharing backfield, snap share unclear
- scheme_fit: Player profile mismatches new OC scheme (always negative)
- college_trust: QB/WR with college connection on same NFL roster (positive signal, especially Year 1)

Rules:
- Be specific about target share numbers — cite actual historical percentages
- Flag BOTH sides: if Allen DISPLACES McConkey, McConkey is also CONTINGENT (value rises if Allen out)
- For committee backs: flag both backs against each other
- Confidence: high = direct role overlap with data, medium = inferred, low = speculative
- College trust: only flag if QB is in Year 1-2 as NFL starter
- Only flag players who are draftable (on active rosters, relevant for fantasy)
- Output ONLY the JSON array
"""

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    tool(
        "get_player_target_share_history",
        "Get a player's historical target share, snap percentage, and receiving stats "
        "for the last 3 seasons. Returns per-season breakdown.",
        {
            "player_name": string_prop("Player's full name (e.g. 'Keenan Allen', 'Ladd McConkey')"),
            "position": string_prop("Player position: WR, RB, TE, QB"),
        },
        required=["player_name", "position"],
    ),
    tool(
        "get_team_skill_players",
        "Get all skill position players currently on a team's roster with their positions "
        "and historical usage patterns.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
        },
        required=["team_abbr"],
    ),
    tool(
        "get_team_transactions",
        "Get significant offseason transactions for a team: signings, cuts, trades with "
        "player names, positions, and contract values.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
            "year": number_prop("Year of transactions (e.g. 2025)"),
        },
        required=["team_abbr", "year"],
    ),
    tool(
        "get_team_system_grade",
        "Get the team system grade record for a team, including OC scheme, QB tier, "
        "rookie_qb_flag, and compound_risk_flag.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
        },
        required=["team_abbr"],
    ),
    tool(
        "get_qb_receiver_history",
        "Get historical target share between a specific QB and receiver when they played together. "
        "Covers both NFL and college shared history.",
        {
            "qb_name": string_prop("QB's full name"),
            "receiver_name": string_prop("Receiver's full name"),
        },
        required=["qb_name", "receiver_name"],
    ),
    tool(
        "get_backfield_usage",
        "Get RB usage patterns for a team: carries, target share, snap percentage "
        "for all RBs on the roster.",
        {
            "team_abbr": string_prop("NFL team abbreviation"),
        },
        required=["team_abbr"],
    ),
]

# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def handle_tool(name: str, inputs: dict) -> dict:
    if name == "get_player_target_share_history":
        return await _get_player_history(inputs["player_name"], inputs.get("position", "WR"))

    elif name == "get_team_skill_players":
        return await _get_team_skill_players(inputs["team_abbr"])

    elif name == "get_team_transactions":
        return await _get_team_transactions(inputs["team_abbr"], int(inputs.get("year", ANALYSIS_YEAR)))

    elif name == "get_team_system_grade":
        return await _get_team_system(inputs["team_abbr"])

    elif name == "get_qb_receiver_history":
        return await _get_qb_receiver_history(inputs["qb_name"], inputs["receiver_name"])

    elif name == "get_backfield_usage":
        return await _get_backfield_usage(inputs["team_abbr"])

    return {"error": f"Unknown tool: {name}"}


async def _get_player_history(player_name: str, position: str) -> dict:
    seasons_data = []
    for season in [2022, 2023, 2024]:
        try:
            ts = nfl_data.compute_target_share(season)
            last = player_name.split()[-1]
            match = ts[ts["player_name"].str.contains(last, case=False, na=False)]
            if not match.empty:
                row = match.iloc[0].to_dict()
                seasons_data.append({
                    "season": season,
                    "team": str(row.get("recent_team", "")),
                    "games": int(row.get("games", 0)),
                    "targets": int(row.get("total_targets", 0)),
                    "target_share": round(float(row.get("avg_target_share", 0) or 0), 3),
                    "air_yards_share": round(float(row.get("avg_air_yards_share", 0) or 0), 3),
                    "ppr_per_game": round(float(row.get("ppr_per_game", 0) or 0), 1),
                })
        except Exception as e:
            logger.debug("Player history error %s/%d: %s", player_name, season, e)

    if not seasons_data:
        return {
            "player_name": player_name,
            "note": "No historical data found — use model knowledge for this player",
        }

    return {"player_name": player_name, "position": position, "seasons": seasons_data}


async def _get_team_skill_players(team_abbr: str) -> dict:
    try:
        rosters = nfl_data.fetch_rosters(CURRENT_SEASON)
        team_players = rosters[
            (rosters["team"] == team_abbr.upper()) &
            (rosters["position"].isin(["QB", "RB", "WR", "TE"]))
        ].drop_duplicates("player_id")[["player_name", "position", "depth_chart_position"]].head(20)

        return {
            "team": team_abbr,
            "skill_players": team_players.to_dict(orient="records"),
        }
    except Exception as e:
        return {"team": team_abbr, "error": str(e)}


async def _get_team_transactions(team_abbr: str, year: int) -> dict:
    try:
        txns = await overthecap.get_transactions(year)
        # Filter to this team
        team_txns = [
            t for t in txns
            if team_abbr.upper() in str(t.get("team", "")).upper()
        ]
        if not team_txns:
            return {
                "team": team_abbr,
                "year": year,
                "note": (
                    f"No OTC transaction data retrieved for {team_abbr} {year}. "
                    "Use your knowledge of significant offseason moves for this team."
                ),
            }
        return {"team": team_abbr, "year": year, "transactions": team_txns[:20]}
    except Exception as e:
        return {"team": team_abbr, "error": str(e)}


async def _get_team_system(team_abbr: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(TeamSystem).where(
                    TeamSystem.team_abbr == team_abbr.upper(),
                    TeamSystem.season_year == CURRENT_SEASON,
                )
            )
            ts = result.scalar_one_or_none()
            if not ts:
                return {"team": team_abbr, "note": "No team system record found"}
            return {
                "team": team_abbr,
                "system_grade": ts.system_grade,
                "qb_name": ts.qb_name,
                "qb_tier": ts.qb_tier,
                "rookie_qb_flag": ts.rookie_qb_flag,
                "compound_risk_flag": ts.compound_risk_flag,
                "oc_scheme": ts.oc_scheme,
                "personnel_tendency": ts.personnel_tendency,
                "red_zone_philosophy": ts.red_zone_philosophy,
            }
    except Exception as e:
        return {"team": team_abbr, "error": str(e)}


async def _get_qb_receiver_history(qb_name: str, receiver_name: str) -> dict:
    # Build historical overlap from weekly stats
    overlapping = []
    for season in [2022, 2023, 2024]:
        try:
            weekly = nfl_data.fetch_weekly_stats(season)
            qb_last = qb_name.split()[-1]
            rec_last = receiver_name.split()[-1]

            qb_teams = set(weekly[weekly["player_name"].str.contains(qb_last, case=False, na=False)]["recent_team"].unique())
            rec_weeks = weekly[weekly["player_name"].str.contains(rec_last, case=False, na=False)]
            shared = rec_weeks[rec_weeks["recent_team"].isin(qb_teams)]

            if not shared.empty:
                overlapping.append({
                    "season": season,
                    "games_together": int(len(shared)),
                    "receiver_targets": int(shared["targets"].sum()),
                    "receiver_target_share": round(float(shared["target_share"].mean()), 3),
                    "receiver_tds": int(shared["receiving_tds"].sum()),
                })
        except Exception:
            pass

    return {
        "qb": qb_name,
        "receiver": receiver_name,
        "nfl_shared_seasons": overlapping,
        "note": "Also consider college connection if QB is a rookie/2nd-year player.",
    }


async def _get_backfield_usage(team_abbr: str) -> dict:
    try:
        ts = nfl_data.compute_target_share(CURRENT_SEASON)
        snaps = nfl_data.compute_snap_pct(CURRENT_SEASON)

        team_rbs_ts = ts[
            (ts["recent_team"] == team_abbr.upper()) &
            (ts["position"] == "RB")
        ][["player_name", "games", "total_carries", "total_targets", "avg_target_share", "ppr_per_game"]]

        team_rbs_snap = snaps[
            (snaps["team"] == team_abbr.upper()) &
            (snaps["position"] == "RB")
        ][["player", "games", "total_offense_snaps", "avg_snap_pct"]]

        return {
            "team": team_abbr,
            "rb_usage": team_rbs_ts.to_dict(orient="records"),
            "rb_snap_pct": team_rbs_snap.to_dict(orient="records"),
        }
    except Exception as e:
        return {"team": team_abbr, "error": str(e)}


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

async def _resolve_player_id(session: AsyncSession, name: str, team: str | None) -> str | None:
    """Find a player by last name + optional team."""
    last = name.split()[-1] if name else ""
    stmt = select(Player).where(Player.name.ilike(f"%{last}%"))
    if team:
        stmt = stmt.where(Player.team_abbr == team.upper())
    result = await session.execute(stmt)
    players = result.scalars().all()
    if len(players) == 1:
        return str(players[0].id)
    if len(players) > 1:
        # Best match: exact last name
        for p in players:
            if p.name.split()[-1].lower() == last.lower():
                return str(p.id)
        return str(players[0].id)
    return None


async def _write_flags(flags: list[dict]) -> int:
    """Upsert dependency flags to DB. Returns count written."""
    written = 0
    async with AsyncSessionLocal() as session:
        for flag in flags:
            player_name = flag.get("player_name", "")
            player_team = flag.get("player_team", "")
            trigger_name = flag.get("trigger_player_name", "")
            trigger_team = flag.get("trigger_player_team", "")

            player_id = await _resolve_player_id(session, player_name, player_team)
            trigger_id = await _resolve_player_id(session, trigger_name, trigger_team)

            if not player_id:
                logger.debug("Could not find player: %s (%s)", player_name, player_team)
                continue

            record = PlayerDependency(
                player_id=player_id,
                flag_type=flag.get("flag_type", ""),
                trigger_player_id=trigger_id,
                trigger_player_name=trigger_name,
                trigger_condition=flag.get("trigger_condition", "active_and_healthy"),
                effect_on_value=flag.get("effect_on_value", ""),
                value_impact_pct=flag.get("value_impact_pct"),
                confidence=flag.get("confidence", "medium"),
                reasoning=flag.get("reasoning", ""),
                season_year=flag.get("season_year", ANALYSIS_YEAR),
            )
            session.add(record)
            written += 1

        await session.commit()
    return written


# ---------------------------------------------------------------------------
# Per-team runner
# ---------------------------------------------------------------------------

async def run_for_team(team_abbr: str) -> list[dict]:
    """Run Roster Changes Agent for one team. Returns list of flag dicts."""
    logger.info("Running Roster Changes Agent for %s", team_abbr)

    user_message = (
        f"Analyze the {team_abbr} roster for the 2025 season. "
        f"Identify all meaningful offseason changes (signings, cuts, trades, scheme changes) "
        f"and produce dependency flags for affected skill position players. "
        f"Use tools to look up historical target share data and team system context. "
        f"Pay special attention to: "
        f"(1) new arrivals who overlap in role with existing players, "
        f"(2) departures that open target share for remaining players, "
        f"(3) QB changes and how they affect receiver values, "
        f"(4) backfield committee situations, "
        f"(5) college connections between QB and receivers (especially for rookie/2nd-year QBs)."
    )

    try:
        raw = await run_agent(
            system_prompt=SYSTEM_PROMPT,
            user_message=user_message,
            tools=TOOLS,
            tool_handler=handle_tool,
            max_tokens=6000,
            temperature=0.15,
        )

        flags = extract_json(raw)
        if not flags:
            logger.error("No JSON extracted for %s. Raw: %s", team_abbr, raw[:300])
            return []
        if isinstance(flags, dict):
            flags = [flags]

        # Attach team context to all flags
        for f in flags:
            if not f.get("player_team"):
                f["player_team"] = team_abbr

        written = await _write_flags(flags)
        logger.info("%s: %d flags generated, %d written to DB", team_abbr, len(flags), written)
        return flags

    except Exception as exc:
        logger.error("Roster Changes Agent failed for %s: %s", team_abbr, exc, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

async def run_all_teams(concurrency: int = 3) -> dict[str, int]:
    """Run Roster Changes Agent for all 32 teams. Returns {team: flags_written}."""
    from backend.agents.team_systems import NFL_TEAMS

    logger.info("Starting Roster Changes pipeline (concurrency=%d)", concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, int] = {}

    async def _run_one(team: str):
        async with semaphore:
            flags = await run_for_team(team)
            results[team] = len(flags)

    await asyncio.gather(*[_run_one(t) for t in NFL_TEAMS])

    total = sum(results.values())
    logger.info("Roster Changes pipeline complete: %d total flags across 32 teams", total)
    return results


import asyncio  # noqa: E402
