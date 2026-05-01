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
from backend.integrations import cfb_data, nfl_data, overthecap
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

        # Draft picks — include college profiles for rookie evaluation context
        draft_picks_context = await self._fetch_draft_picks_context(team)

        return {
            "team": team,
            "season": analysis_year,
            "system_grade": system_grade,
            "transactions": transactions,
            "current_roster": roster,
            "target_share_history": target_shares,
            "backfield_usage": backfield,
            "qb_receiver_history": qb_histories,
            "draft_picks": draft_picks_context,
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
    # Draft pick helpers (Step 6 — non-destructive additions)
    # ------------------------------------------------------------------

    async def _fetch_draft_picks_context(self, team: str) -> list[dict]:
        """
        Return compact college profile + capital value for each draft pick
        on this team. Pre-aggregated for the model prompt — no extra API call.
        """
        current_season = get_current_season()
        try:
            picks_df = nfl_data.fetch_nfl_draft_picks(current_season)
        except Exception as exc:
            logger.warning("Could not fetch draft picks for %d: %s", current_season, exc)
            return []

        if picks_df.empty:
            return []

        # Normalize team column name
        team_col = next((c for c in ("team", "nfl_team") if c in picks_df.columns), None)
        if not team_col:
            return []

        team_picks = picks_df[picks_df[team_col].str.upper() == team.upper()]
        if team_picks.empty:
            return []

        college_df = _get_cached_data("college_target_share")

        result = []
        for _, pick in team_picks.iterrows():
            player_name = str(pick.get("player_name", pick.get("player", "")))
            position    = str(pick.get("position", ""))
            draft_round = int(pick.get("round", 7))
            pick_num    = int(pick.get("pick_number", pick.get("pick", 200)))

            capital_val    = nfl_data.get_draft_capital_value(draft_round, pick_num)
            capital_signal = nfl_data.get_capital_signal(capital_val)

            college_row: dict = {}
            if college_df is not None and not college_df.empty:
                last = player_name.split()[-1].lower() if player_name else ""
                matches = college_df[
                    college_df["player_name"].str.lower().str.contains(last, na=False)
                ] if last else type(college_df)()
                if not matches.empty:
                    row = matches.iloc[0]
                    raw_dom = float(row.get("dominator_rating", 0) or 0)
                    conf    = str(row.get("conference", "Unknown"))
                    adj_dom = cfb_data.get_adjusted_dominator(raw_dom, conf)
                    college_row = {
                        "dominator_rating":    raw_dom,
                        "adjusted_dominator":  adj_dom,
                        "yards_per_route_run": float(row.get("yards_per_route_run", 0) or 0),
                        "conference":          conf,
                    }

            result.append({
                "player_name":      player_name,
                "position":         position,
                "round":            draft_round,
                "pick_number":      pick_num,
                "capital_value":    capital_val,
                "capital_signal":   capital_signal,
                "college_profile":  college_row,
            })

        return result

    def _get_college_profile(self, player_name: str, position: str) -> dict:
        """Look up college profile from the shared college_target_share cache."""
        college_df = _get_cached_data("college_target_share")
        if college_df is None or college_df.empty:
            return {}
        last = player_name.split()[-1].lower() if player_name else ""
        matches = college_df[
            college_df["player_name"].str.lower().str.contains(last, na=False)
        ] if last else college_df.iloc[0:0]
        if matches.empty:
            return {}
        row = matches.iloc[0]
        return {
            "dominator_rating":    float(row.get("dominator_rating", 0) or 0),
            "yards_per_route_run": float(row.get("yards_per_route_run", 0) or 0),
            "conference":          str(row.get("conference", "Unknown")),
        }

    def _find_historical_comps(
        self,
        comp_table,
        position: str,
        adjusted_dominator: float,
        capital_value: float,
        age_at_draft: int = 22,
    ) -> list[dict]:
        """Find 3-5 most similar drafted prospects from the historical comp table."""
        import pandas as pd
        if comp_table is None or (hasattr(comp_table, "empty") and comp_table.empty):
            return []
        filtered = comp_table[comp_table["position"] == position].copy()
        if filtered.empty:
            return []
        # Similarity score: weighted distance on adjusted_dominator and capital_value
        filtered = filtered.copy()
        filtered["dom_dist"]  = (filtered["adjusted_dominator"] - adjusted_dominator).abs()
        filtered["cap_dist"]  = (filtered["capital_value"] - capital_value).abs() / 100
        filtered["sim_score"] = filtered["dom_dist"] + filtered["cap_dist"] * 0.5
        top = filtered.nsmallest(5, "sim_score")
        return [
            {
                "name":    str(row["player_name"]),
                "yr1_ppg": float(row["yr1_ppg"]) if pd.notna(row.get("yr1_ppg")) else None,
                "yr2_ppg": float(row["yr2_ppg"]) if pd.notna(row.get("yr2_ppg")) else None,
            }
            for _, row in top.iterrows()
        ]

    def _get_landing_spot_modifier(self, system_grade_dict: dict) -> float:
        """Scale landing spot from 0.75 (compound risk) to 1.18 (elite system)."""
        if system_grade_dict.get("compound_risk_flag"):
            return 0.75
        if system_grade_dict.get("rookie_qb_flag"):
            return 0.85
        grade = str(system_grade_dict.get("system_grade", "C"))
        return {"A": 1.18, "B": 1.08, "C": 1.00, "D": 0.88, "F": 0.78}.get(
            grade[0].upper(), 1.00
        )

    def _grade_college_profile(
        self, adjusted_dominator: float, yards_per_route: float, position: str
    ) -> str:
        """Grade college profile: elite / strong / average / weak."""
        if position == "RB":
            # RBs: use adjusted_dominator as usage_rate proxy
            if adjusted_dominator >= 0.35:
                return "elite"
            if adjusted_dominator >= 0.25:
                return "strong"
            if adjusted_dominator >= 0.15:
                return "average"
            return "weak"
        # WR / TE
        if adjusted_dominator >= 0.38 and yards_per_route >= 2.8:
            return "elite"
        if adjusted_dominator >= 0.30 or yards_per_route >= 2.5:
            return "strong"
        if adjusted_dominator >= 0.22:
            return "average"
        return "weak"

    async def _write_rookie_evaluation(self, fields: dict) -> None:
        """Write rookie evaluation fields from _handle_draft_pick to the players table."""
        player_name = fields.get("player_name", "")
        if not player_name:
            return

        async with AsyncSessionLocal() as session:
            last = player_name.split()[-1].lower()
            from sqlalchemy import or_
            result = await session.execute(
                select(Player).where(Player.name.ilike(f"%{last}%"))
            )
            candidates = result.scalars().all()
            if not candidates:
                logger.debug("Could not find player for rookie eval: %s", player_name)
                return

            # Match by name similarity
            player = next(
                (p for p in candidates if last in p.name.lower()), candidates[0]
            )

            from decimal import Decimal
            player.is_rookie             = True
            player.college_profile_grade = fields.get("college_profile_grade")
            player.draft_capital_signal  = fields.get("draft_capital_signal")
            if fields.get("draft_capital_value") is not None:
                player.draft_capital_value = Decimal(str(fields["draft_capital_value"]))
            if fields.get("adjusted_dominator_rating") is not None:
                player.adjusted_dominator_rating = Decimal(str(fields["adjusted_dominator_rating"]))
            player.conference            = fields.get("conference")
            player.historical_comp_names = fields.get("historical_comp_names", [])
            if fields.get("comp_yr1_avg_ppg") is not None:
                player.comp_yr1_avg_ppg = Decimal(str(fields["comp_yr1_avg_ppg"]))
            if fields.get("comp_yr2_avg_ppg") is not None:
                player.comp_yr2_avg_ppg = Decimal(str(fields["comp_yr2_avg_ppg"]))
            if fields.get("landing_spot_modifier") is not None:
                player.landing_spot_modifier = Decimal(str(fields["landing_spot_modifier"]))
            player.projection_confidence = fields.get("projection_confidence", "low")
            player.variance_flag         = bool(fields.get("variance_flag", True))

            await session.commit()
            logger.debug("Wrote rookie evaluation for %s", player_name)

    async def _generate_rookie_displacement_flags(
        self,
        pick: dict,
        position: str,
        capital_signal: str,
        team_context: dict,
    ) -> list[dict]:
        """
        Generate DISPLACED + CONTINGENT flags for incumbents threatened by a draft pick.
        High capital (rounds 1-2): generate flags.
        Medium capital (rounds 3-4): lower confidence.
        Low capital (rounds 5-7): no flags.
        """
        if capital_signal == "low":
            return []

        flags = []
        for incumbent in team_context.get("current_roster", []):
            if incumbent.get("position") != position:
                continue
            if incumbent.get("name") == pick.get("player_name"):
                continue

            impact_pct  = -0.25 if capital_signal == "high" else -0.15
            confidence  = "medium" if capital_signal == "high" else "low"
            player_name = pick.get("player_name", "")
            inc_name    = incumbent.get("name", "")

            flags.append({
                "player_name":         inc_name,
                "player_team":         team_context.get("team", ""),
                "player_position":     position,
                "flag_type":           "displaced",
                "trigger_player_name": player_name,
                "trigger_player_team": team_context.get("team", ""),
                "trigger_condition":   "active_and_healthy",
                "effect_on_value":     "negative",
                "value_impact_pct":    impact_pct,
                "confidence":          confidence,
                "reasoning":           (
                    f"{player_name} drafted round {pick.get('round')} "
                    f"({capital_signal} capital). Will compete for {position} role."
                ),
                "season_year":         get_analysis_year(),
            })
            flags.append({
                "player_name":         inc_name,
                "player_team":         team_context.get("team", ""),
                "player_position":     position,
                "flag_type":           "contingent",
                "trigger_player_name": player_name,
                "trigger_player_team": team_context.get("team", ""),
                "trigger_condition":   "injured_or_absent",
                "effect_on_value":     "positive",
                "value_impact_pct":    abs(impact_pct) * 0.8,
                "confidence":          confidence,
                "reasoning":           (
                    f"{inc_name} value recovers if {player_name} misses time."
                ),
                "season_year":         get_analysis_year(),
            })
        return flags

    async def _handle_draft_pick(
        self,
        pick: dict,
        team_context: dict,
        comp_table,
    ) -> list[dict]:
        """
        Full prospect evaluation for one NFL draft pick.
        Writes rookie evaluation fields to the player record.
        Returns displacement flags for incumbents.
        """
        player_name = pick.get("player_name", "")
        position    = pick.get("position", "")

        college_profile = self._get_college_profile(player_name, position)
        capital_value   = nfl_data.get_draft_capital_value(
            pick.get("round", 7), pick.get("pick_number", 200)
        )
        capital_signal  = nfl_data.get_capital_signal(capital_value)

        raw_dom = college_profile.get("dominator_rating", 0.0)
        conf    = college_profile.get("conference", "Unknown")
        adj_dom = cfb_data.get_adjusted_dominator(raw_dom, conf)

        comps        = self._find_historical_comps(
            comp_table, position, adj_dom, capital_value,
            age_at_draft=pick.get("age_at_draft", 22),
        )
        landing_mod  = self._get_landing_spot_modifier(
            team_context.get("system_grade", {})
        )
        profile_grade = self._grade_college_profile(
            adj_dom,
            college_profile.get("yards_per_route_run", 0.0),
            position,
        )

        yr1_ppg = (
            sum(c["yr1_ppg"] for c in comps if c.get("yr1_ppg") is not None)
            / max(1, sum(1 for c in comps if c.get("yr1_ppg") is not None))
        ) if comps else None
        yr2_ppg = (
            sum(c["yr2_ppg"] for c in comps if c.get("yr2_ppg") is not None)
            / max(1, sum(1 for c in comps if c.get("yr2_ppg") is not None))
        ) if comps else None

        await self._write_rookie_evaluation({
            "player_name":            player_name,
            "is_rookie":              True,
            "college_profile_grade":  profile_grade,
            "draft_capital_signal":   capital_signal,
            "draft_capital_value":    round(capital_value, 1),
            "adjusted_dominator_rating": round(adj_dom, 3),
            "conference":             conf,
            "historical_comp_names":  [c["name"] for c in comps[:3]],
            "comp_yr1_avg_ppg":       round(yr1_ppg, 2) if yr1_ppg else None,
            "comp_yr2_avg_ppg":       round(yr2_ppg, 2) if yr2_ppg else None,
            "landing_spot_modifier":  round(landing_mod, 3),
            "projection_confidence":  "low",
            "variance_flag":          True,
        })

        return await self._generate_rookie_displacement_flags(
            pick, position, capital_signal, team_context
        )

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

            # Draft pick prospect evaluation — Python-computed, no extra API call
            comp_table = _get_cached_data("historical_comp_table")
            draft_picks = context.get("draft_picks", [])
            for pick in draft_picks:
                try:
                    rookie_flags = await self._handle_draft_pick(pick, context, comp_table)
                    flags.extend(rookie_flags)
                except Exception as exc:
                    logger.warning(
                        "Draft pick evaluation failed for %s: %s",
                        pick.get("player_name"), exc,
                    )

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

        # Pre-load college data for draft pick evaluation
        try:
            college_seasons = list(range(current_season - 6, current_season))
            logger.info("Pre-loading college target share for seasons %s...", college_seasons)
            college_df = cfb_data.get_college_target_share(college_seasons)
            _set_cached_data("college_target_share", college_df)
        except Exception as exc:
            logger.warning("Could not pre-load college data: %s", exc)

        # Pre-load historical comp table (expensive — cached aggressively)
        try:
            logger.info("Pre-loading historical comp table...")
            comp_table = cfb_data.build_historical_comp_table()
            _set_cached_data("historical_comp_table", comp_table)
            logger.info("Historical comp table: %d records", len(comp_table))
        except Exception as exc:
            logger.warning("Could not build historical comp table: %s", exc)

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
    """Replace all dependency flags for the affected players in one DB transaction.

    Delete-then-insert ensures re-runs (including cache hits) are idempotent.
    All flags in one batch belong to the same team, so we delete by resolved
    player IDs + season_year before inserting the fresh set.
    """
    if not flags:
        return 0

    from sqlalchemy import delete as sa_delete
    analysis_year = get_analysis_year()

    async with AsyncSessionLocal() as session:
        # Collect all names for bulk resolution
        names_and_teams: list[tuple[str, str | None]] = []
        for f in flags:
            names_and_teams.append((f.get("player_name", ""), f.get("player_team")))
            names_and_teams.append((f.get("trigger_player_name", ""), f.get("trigger_player_team")))

        id_map = await _bulk_resolve_player_ids(session, names_and_teams)

        # Collect the player IDs that will be written so we can purge stale rows first
        player_ids_in_batch: set = set()
        for flag in flags:
            pid = id_map.get((flag.get("player_name", ""), flag.get("player_team")))
            if pid:
                player_ids_in_batch.add(pid)

        # Delete existing flags for these players / season to prevent duplicates on re-run
        if player_ids_in_batch:
            await session.execute(
                sa_delete(PlayerDependency).where(
                    PlayerDependency.player_id.in_(player_ids_in_batch),
                    PlayerDependency.season_year == analysis_year,
                )
            )

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
