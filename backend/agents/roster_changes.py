"""
Agent 2: Roster Changes Agent

Analyzes offseason transactions to identify dependency flags for all draftable players.
The canonical test case: Keenan Allen signing with LAC → McConkey DISPLACED + CONTINGENT.

Architecture:
  - Model: Sonnet (causal chain-of-reasoning — the ONLY pre-draft agent using Sonnet)
  - Max tokens: 2000 per team
  - Pattern: pre-aggregate in Python → ONE call_once() per team → parse JSON → bulk DB write
  - DISPLACED always paired with CONTINGENT — never flag one without the other

Flag types:
  displaced   — role directly overlapped by a new arrival (negative)
  contingent  — value rises when trigger player is absent (always paired with displaced)
  beneficiary — value rises when another player departs
  committee   — RB sharing backfield, unclear snap distribution
  scheme_fit  — player profile mismatches new OC tendency
  college_trust — QB/WR college connection on same roster (positive modifier)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, SONNET
from backend.database import AsyncSessionLocal
from backend.integrations import nfl_data, overthecap
from backend.models.dependency import PlayerDependency
from backend.models.player import Player
from backend.utils.seasons import get_analysis_seasons, get_analysis_year, get_current_season

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared data cache — loaded once in run_all_teams(), reused per team
# ---------------------------------------------------------------------------

_DATA_CACHE: dict = {}


def _get_cached_data(key: str):
    return _DATA_CACHE.get(key)


def _set_cached_data(key: str, value) -> None:
    _DATA_CACHE[key] = value


# ---------------------------------------------------------------------------
# System prompt — dynamic year, no hardcoded integers
# ---------------------------------------------------------------------------

def _build_system_prompt() -> str:
    analysis_year = get_analysis_year()
    return f"""You are a fantasy football analyst specializing in offseason transaction analysis.
You will receive pre-aggregated data for one NFL team.
Analyze the data and produce dependency flags for affected skill position players.

Pay special attention to:
1. New arrivals who overlap in role with existing players (displaced flag)
2. Departures that open target share (beneficiary flag)
3. QB changes affecting receiver values
4. Backfield committee situations (committee flag on BOTH backs)
5. College connections when QB is rookie/2nd-year (college_trust flag)

CRITICAL RULE — always flag BOTH sides of a displacement:
- The displaced player gets a "displaced" flag (negative, trigger_condition=active_and_healthy)
- The SAME player ALSO gets a "contingent" flag (positive, trigger_condition=injured)
Never produce a displaced flag without its matching contingent flag.

Output ONLY a valid JSON array. No explanation, no preamble, no markdown fences.
Your entire response must be parseable by json.loads().
Each element must match this schema exactly:
{{
  "player_name": string,
  "player_team": string,
  "player_position": string,
  "flag_type": "displaced|contingent|beneficiary|committee|scheme_fit|college_trust",
  "trigger_player_name": string,
  "trigger_player_team": string,
  "trigger_condition": "active_and_healthy|injured|absent|traded",
  "effect_on_value": "negative|positive|neutral",
  "value_impact_pct": number,
  "confidence": "high|medium|low",
  "reasoning": string,
  "season_year": {analysis_year}
}}"""


# ---------------------------------------------------------------------------
# RosterChangesAgent
# ---------------------------------------------------------------------------

class RosterChangesAgent(BaseAgent):
    AGENT_NAME       = "roster_changes"
    AGENT_MODEL      = SONNET
    AGENT_MAX_TOKENS = 4000

    # ------------------------------------------------------------------
    # Data pre-aggregation — all Python, zero API calls
    # ------------------------------------------------------------------

    async def _build_team_context(self, team_abbr: str) -> dict:
        """
        Pre-fetch and aggregate ALL data for one team's dependency analysis.
        No API calls here — only nfl_data_py, OTC, and DB lookups.
        Returns a compact summary dict ready to pass to the model.
        """
        team = team_abbr.upper()
        analysis_year = get_analysis_year()

        transactions   = await self._fetch_transactions(team, analysis_year)
        roster         = await self._fetch_skill_roster(team)
        target_shares  = await self._fetch_target_shares(roster)
        backfield      = await self._fetch_backfield(team)
        qb_histories   = await self._fetch_qb_histories(team, roster)
        system_grade   = await self._fetch_team_system(team)

        return {
            "team": team,
            "season": analysis_year,
            "system_grade": system_grade,
            "transactions": transactions,
            "current_roster": roster,
            "target_share_history": target_shares,
            "backfield_usage": backfield,
            "qb_receiver_history": qb_histories,
        }

    async def _fetch_transactions(self, team: str, analysis_year: int) -> list[dict]:
        try:
            return overthecap.get_transactions_summary(team, analysis_year)
        except Exception as exc:
            logger.warning("Transactions unavailable for %s: %s", team, exc)
            return []

    async def _fetch_skill_roster(self, team: str) -> list[dict]:
        try:
            return overthecap.get_skill_roster_summary(team)
        except Exception as exc:
            logger.warning("Skill roster unavailable for %s: %s", team, exc)
            return []

    async def _fetch_target_shares(self, roster: list[dict]) -> dict:
        """
        Build target share history for all players on the roster.
        Uses _DATA_CACHE — compute_target_share loads once per season,
        then slices it per player. Never reloads the full dataset.
        """
        result: dict[str, list[dict]] = {}
        analysis_seasons = get_analysis_seasons(3)

        for season in analysis_seasons:
            cache_key = f"target_share_{season}"
            ts_df = _get_cached_data(cache_key)

            if ts_df is None:
                try:
                    ts_df = nfl_data.compute_target_share(season)
                    _set_cached_data(cache_key, ts_df)
                except Exception as exc:
                    logger.warning("Could not load target share %d: %s", season, exc)
                    continue

            for player in roster:
                name = player.get("name", "")
                last = name.split()[-1] if name else ""
                if not last:
                    continue

                match = ts_df[ts_df["player_name"].str.contains(last, case=False, na=False)]
                if match.empty:
                    continue

                row = match.iloc[0]
                if name not in result:
                    result[name] = []

                result[name].append({
                    "season": season,
                    "team": str(row.get("recent_team", "")),
                    "games": int(row.get("games", 0)),
                    "targets": int(row.get("total_targets", 0)),
                    "target_share": round(float(row.get("avg_target_share", 0) or 0), 3),
                    "air_yards_share": round(float(row.get("avg_air_yards_share", 0) or 0), 3),
                })

        return result

    async def _fetch_backfield(self, team: str) -> dict:
        current_season = get_current_season()
        cache_key = f"target_share_{current_season}"
        ts_df = _get_cached_data(cache_key)

        if ts_df is None:
            try:
                ts_df = nfl_data.compute_target_share(current_season)
                _set_cached_data(cache_key, ts_df)
            except Exception as exc:
                return {"error": str(exc)}

        rbs = ts_df[
            (ts_df["recent_team"] == team) & (ts_df["position"] == "RB")
        ][["player_name", "games", "total_carries", "total_targets", "avg_target_share"]]

        return {"team": team, "rb_usage": rbs.to_dict(orient="records")}

    async def _fetch_qb_histories(self, team: str, roster: list[dict]) -> list[dict]:
        qbs       = [p for p in roster if p.get("position") == "QB"]
        receivers = [p for p in roster if p.get("position") in ("WR", "TE")]

        if not qbs or not receivers:
            return []

        histories: list[dict] = []
        analysis_seasons = get_analysis_seasons(3)

        for season in analysis_seasons:
            cache_key = f"weekly_stats_{season}"
            weekly = _get_cached_data(cache_key)
            if weekly is None:
                try:
                    weekly = nfl_data.fetch_weekly_stats(season)
                    _set_cached_data(cache_key, weekly)
                except Exception:
                    continue

            for qb in qbs:
                qb_last = qb["name"].split()[-1]
                qb_teams = set(
                    weekly[weekly["player_name"].str.contains(qb_last, case=False, na=False)]
                    ["recent_team"].unique()
                )

                for rec in receivers:
                    rec_last = rec["name"].split()[-1]
                    shared = weekly[
                        weekly["player_name"].str.contains(rec_last, case=False, na=False) &
                        weekly["recent_team"].isin(qb_teams)
                    ]
                    if not shared.empty:
                        histories.append({
                            "qb": qb["name"],
                            "receiver": rec["name"],
                            "season": season,
                            "games_together": int(len(shared)),
                            "receiver_target_share": round(
                                float(shared["target_share"].mean()), 3
                            ),
                        })

        return histories

    async def _fetch_team_system(self, team: str) -> dict:
        from backend.models.team_system import TeamSystem

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(TeamSystem).where(TeamSystem.team_abbr == team)
            )
            system = result.scalar_one_or_none()
            if system:
                return {
                    "qb_tier": system.qb_tier,
                    "qb_name": system.qb_name,
                    "rookie_qb_flag": system.rookie_qb_flag,
                    "compound_risk_flag": system.compound_risk_flag,
                    "oc_scheme": system.oc_scheme,
                    "system_grade": system.system_grade,
                }
            return {}

    # ------------------------------------------------------------------
    # Per-team runner — exactly ONE call_once()
    # ------------------------------------------------------------------

    async def run_for_team(self, team_abbr: str) -> list[dict]:
        """Run for one team. One Sonnet call. Returns list of flag dicts."""
        logger.info("Building context for %s", team_abbr)

        try:
            context = await self._build_team_context(team_abbr)
            system_prompt = _build_system_prompt()

            raw = await self.call_once(
                system=system_prompt,
                user=(
                    f"Analyze the following pre-aggregated data for the "
                    f"{context['team']} roster and produce dependency flags:\n\n"
                    f"{json.dumps(context, default=str)}"
                ),
                input_data=context,
                entity_id=team_abbr.upper(),
            )

            if not raw:
                return []  # dry_run

            flags = parse_json_output(raw)
            if isinstance(flags, dict):
                flags = [flags]

            # Ensure player_team is set
            for f in flags:
                if not f.get("player_team"):
                    f["player_team"] = team_abbr.upper()

            written = await _write_flags(flags)
            logger.info(
                "%s: %d flags generated, %d written", team_abbr, len(flags), written
            )
            return flags

        except Exception as exc:
            logger.error("Roster Changes failed for %s: %s", team_abbr, exc, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Full pipeline — pre-warm caches once, then run all 32 teams
    # ------------------------------------------------------------------

    async def run_all_teams(self, concurrency: int = 2) -> dict[str, int]:
        """
        Pre-loads shared data caches ONCE before concurrent team runs.
        Returns {team_abbr: flag_count}.
        """
        from backend.agents.team_systems import NFL_TEAMS

        analysis_seasons = get_analysis_seasons(3)
        current_season   = get_current_season()
        analysis_year    = get_analysis_year()

        # Pre-load OTC transactions once for all teams
        logger.info("Pre-loading OTC transactions for %d...", analysis_year)
        await overthecap.preload_transactions([analysis_year])

        logger.info("Pre-loading NFL data caches for seasons %s...", analysis_seasons)
        for season in analysis_seasons:
            try:
                ts = nfl_data.compute_target_share(season)
                _set_cached_data(f"target_share_{season}", ts)
                weekly = nfl_data.fetch_weekly_stats(season)
                _set_cached_data(f"weekly_stats_{season}", weekly)
                logger.info("Cached season %d data", season)
            except Exception as exc:
                logger.warning("Could not pre-load season %d: %s", season, exc)

        # Also cache current season for backfield (may overlap with analysis_seasons)
        if f"target_share_{current_season}" not in _DATA_CACHE:
            try:
                ts = nfl_data.compute_target_share(current_season)
                _set_cached_data(f"target_share_{current_season}", ts)
            except Exception as exc:
                logger.warning("Could not pre-load current season target share: %s", exc)

        logger.info(
            "Starting Roster Changes pipeline (concurrency=%d)", concurrency
        )
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, int] = {}

        async def _run_one(team: str) -> None:
            async with semaphore:
                flags = await self.run_for_team(team)
                results[team] = len(flags)

        await asyncio.gather(*[_run_one(t) for t in NFL_TEAMS])

        total = sum(results.values())
        logger.info("Complete: %d total flags across 32 teams", total)
        return results


# ---------------------------------------------------------------------------
# Bulk DB helpers — module level (not agent state)
# ---------------------------------------------------------------------------

async def _bulk_resolve_player_ids(
    session: AsyncSession,
    names_and_teams: list[tuple[str, str | None]],
) -> dict[tuple, str | None]:
    """
    Resolve all player names in ONE query.
    Returns {(name, team): player_id}.
    """
    results: dict[tuple, str | None] = {}
    unique_lasts = {name.split()[-1] for name, _ in names_and_teams if name}
    if not unique_lasts:
        return results

    from sqlalchemy import or_
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
            if team:
                match = [p for p in candidates if p.team_abbr == team.upper()]
                if match:
                    results[(name, team)] = str(match[0].id)
                    continue
            results[(name, team)] = str(candidates[0].id)

    return results


async def _write_flags(flags: list[dict]) -> int:
    """Bulk upsert — one DB transaction for all flags from one team."""
    if not flags:
        return 0

    analysis_year = get_analysis_year()

    async with AsyncSessionLocal() as session:
        # Collect all names for bulk resolution
        names_and_teams: list[tuple[str, str | None]] = []
        for f in flags:
            names_and_teams.append((f.get("player_name", ""), f.get("player_team")))
            names_and_teams.append((f.get("trigger_player_name", ""), f.get("trigger_player_team")))

        id_map = await _bulk_resolve_player_ids(session, names_and_teams)

        written = 0
        for flag in flags:
            player_name  = flag.get("player_name", "")
            player_team  = flag.get("player_team")
            trigger_name = flag.get("trigger_player_name", "")
            trigger_team = flag.get("trigger_player_team")

            player_id  = id_map.get((player_name, player_team))
            trigger_id = id_map.get((trigger_name, trigger_team))

            if not player_id:
                logger.debug("Could not resolve player: %s (%s)", player_name, player_team)
                continue

            session.add(PlayerDependency(
                player_id=player_id,
                flag_type=flag.get("flag_type", ""),
                trigger_player_id=trigger_id,
                trigger_player_name=trigger_name,
                trigger_condition=flag.get("trigger_condition", "active_and_healthy"),
                effect_on_value=flag.get("effect_on_value", ""),
                value_impact_pct=flag.get("value_impact_pct"),
                confidence=flag.get("confidence", "medium"),
                reasoning=flag.get("reasoning", ""),
                season_year=flag.get("season_year", analysis_year),
            ))
            written += 1

        await session.commit()

    return written


# ---------------------------------------------------------------------------
# Module-level compatibility shims (used by pipeline.py and scripts)
# ---------------------------------------------------------------------------

_agent_instance: RosterChangesAgent | None = None


def _get_agent(dry_run: bool = False) -> RosterChangesAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = RosterChangesAgent(dry_run=dry_run)
    return _agent_instance


async def run_for_team(team_abbr: str, dry_run: bool = False) -> list[dict]:
    return await _get_agent(dry_run).run_for_team(team_abbr)


async def run_all_teams(concurrency: int = 4, dry_run: bool = False) -> dict[str, int]:
    return await _get_agent(dry_run).run_all_teams(concurrency)
