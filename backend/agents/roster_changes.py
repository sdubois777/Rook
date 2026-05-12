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
from datetime import datetime, timezone
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, SONNET
from backend.database import AsyncSessionLocal
from backend.integrations import cfb_data, nfl_comp_builder, nfl_data, overthecap
from backend.models.dependency import PlayerDependency
from backend.models.player import Player
from backend.utils.seasons import get_analysis_seasons, get_analysis_year, get_current_season

logger = logging.getLogger(__name__)

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

FLAG SELECTION RULES — committee vs displaced (mutual exclusivity):
- COMMITTEE: RB-only. True timeshare where BOTH backs project 40-50% of carries and neither dominates.
  Only use when incoming and incumbent have SIMILAR carry profiles (both workhorse-type or both committee-type).
  DO NOT flag committee when a specialist back (pass-catching/change-of-pace) joins a workhorse.
  A workhorse + receiving specialist pairing is complementary, NOT a committee.
  Example of TRUE committee: Eagles Swift/Robinson split — neither back had 55%+ carries.
  Example of NOT a committee: Derrick Henry + Justice Hill — Henry is a workhorse, Hill is a pass-catcher.
- DISPLACED: Any position. Use when incoming back takes a portion of workload from incumbent, even if
  the incumbent remains the lead back. For specialist pairings (workhorse + receiving back), use displaced
  with MILD impact (-8% to -12%), not committee.
- NEVER assign both committee AND displaced to the same player for the same trigger.
- Decision tree: incoming clearly superior → DISPLACED (high impact). Specialist pairing → DISPLACED (mild impact -8 to -12%). True 50/50 split → COMMITTEE.
- Non-RB positions (WR/TE/QB) NEVER get committee — always use displaced.

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
# Flag validation — reject garbage data before DB write
# ---------------------------------------------------------------------------

def validate_flag(flag: dict) -> bool:
    """Reject flags missing required fields. A flag without a trigger is meaningless."""
    if not (flag.get("trigger_player_name") or "").strip():
        logger.warning(
            "Rejected flag for %s: missing trigger_player_name. Flag type: %s",
            flag.get("player_name"), flag.get("flag_type"),
        )
        return False
    if not flag.get("flag_type"):
        logger.warning("Rejected flag: missing flag_type")
        return False
    if flag.get("value_impact_pct") is None:
        logger.warning(
            "Rejected flag for %s: missing value_impact_pct",
            flag.get("player_name"),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Post-processing: dedup + mutual exclusivity
# ---------------------------------------------------------------------------

def deduplicate_flags(flags: list[dict]) -> list[dict]:
    """Remove duplicate and conflicting flags.

    Rules:
    1. When displaced exists for a player+trigger, remove any beneficiary
       for the same pair (displaced+contingent supersedes beneficiary).
    2. Remove exact duplicates (same player+trigger+flag_type).
    """
    # Step 1: beneficiary superseded by displaced for same player+trigger
    displaced_pairs = {
        (f["player_name"], f.get("trigger_player_name", ""))
        for f in flags
        if f.get("flag_type") == "displaced"
    }

    flags = [
        f for f in flags
        if not (
            f.get("flag_type") == "beneficiary"
            and (f["player_name"], f.get("trigger_player_name", "")) in displaced_pairs
        )
    ]

    # Step 2: deduplicate by (player_name, trigger_player_name, flag_type)
    seen: set[tuple] = set()
    result = []
    for f in flags:
        key = (
            f.get("player_name", ""),
            f.get("trigger_player_name", ""),
            f.get("flag_type", ""),
        )
        if key not in seen:
            seen.add(key)
            result.append(f)

    removed = len(flags) - len(result)
    if removed:
        logger.info("Deduplicated %d flags", removed)
    return result


def downgrade_specialist_committee_flags(flags: list[dict], backfield_usage: dict | None = None) -> list[dict]:
    """Convert committee flags to displaced when one back clearly dominates.

    A workhorse + specialist pairing is NOT a committee. If the incumbent has
    significantly more carries than the trigger player, convert committee to
    displaced with mild impact (-10%).
    """
    if not backfield_usage:
        return flags

    rb_carries: dict[str, int] = {}
    for rb in backfield_usage.get("rb_usage", []):
        name = rb.get("player_name", "")
        rb_carries[name] = int(rb.get("total_carries", 0))

    converted = 0
    for f in flags:
        if f.get("flag_type") != "committee" or f.get("player_position", "").upper() != "RB":
            continue

        player_name = f.get("player_name", "")
        trigger_name = f.get("trigger_player_name", "")

        player_carries = rb_carries.get(player_name, 0)
        trigger_carries = rb_carries.get(trigger_name, 0)

        # If the incumbent has 2x+ more carries → not a true committee
        if player_carries > 0 and trigger_carries > 0:
            if player_carries >= trigger_carries * 2 or trigger_carries >= player_carries * 2:
                f["flag_type"] = "displaced"
                f["effect_on_value"] = "negative"
                f["value_impact_pct"] = max(f.get("value_impact_pct", -10), -12)
                f["reasoning"] = (
                    f"{trigger_name} is a complementary/specialist back, not a true committee threat. "
                    f"Carry split ({player_carries} vs {trigger_carries}) shows one back dominates. "
                    f"Minor workload impact, not a timeshare."
                )
                converted += 1
        elif player_carries > 150 and trigger_carries == 0:
            # Incumbent is established workhorse, trigger is new arrival with no carry history
            f["flag_type"] = "displaced"
            f["effect_on_value"] = "negative"
            f["value_impact_pct"] = max(f.get("value_impact_pct", -10), -10)
            f["reasoning"] = (
                f"{trigger_name} arrives as a specialist/backup behind established workhorse "
                f"{player_name} ({player_carries} carries). Minor workload displacement, not a committee."
            )
            converted += 1

    if converted:
        logger.info("Converted %d committee flags to displaced (specialist pairing)", converted)
    return flags


def enforce_flag_mutual_exclusivity(flags: list[dict]) -> list[dict]:
    """Remove committee flags that conflict with displaced flags for the same trigger.

    Rules:
    1. Non-RB positions never get committee — convert to displaced.
    2. If both committee and displaced exist for the same player+trigger,
       keep displaced and drop committee.
    """
    # Step 1: Convert non-RB committee to displaced
    for f in flags:
        if (
            f.get("flag_type") == "committee"
            and f.get("player_position", "").upper() not in ("RB",)
        ):
            f["flag_type"] = "displaced"
            if f.get("effect_on_value") == "neutral":
                f["effect_on_value"] = "negative"

    # Step 2: Remove committee where displaced exists for same player+trigger
    # Build a set of (player_name, trigger_player_name) that have displaced flags
    displaced_pairs = {
        (f["player_name"], f.get("trigger_player_name", ""))
        for f in flags
        if f.get("flag_type") == "displaced"
    }

    result = [
        f for f in flags
        if not (
            f.get("flag_type") == "committee"
            and (f["player_name"], f.get("trigger_player_name", "")) in displaced_pairs
        )
    ]

    removed = len(flags) - len(result)
    if removed:
        logger.info("Removed %d duplicate committee flags (displaced exists for same trigger)", removed)
    return result


# ---------------------------------------------------------------------------
# RosterChangesAgent
# ---------------------------------------------------------------------------

class RosterChangesAgent(BaseAgent):
    AGENT_NAME       = "roster_changes"
    AGENT_MODEL      = SONNET
    AGENT_MAX_TOKENS = 4000

    def __init__(self, dry_run: bool = False, warehouse=None):
        super().__init__(dry_run=dry_run, warehouse=warehouse)
        self._college_cache: dict = {}

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
        Reads from warehouse — data loaded once before pipeline starts.
        """
        result: dict[str, list[dict]] = {}
        analysis_seasons = get_analysis_seasons(3)

        for season in analysis_seasons:
            ts_df = self._warehouse.get_target_share(season)
            if ts_df is None or (isinstance(ts_df, pd.DataFrame) and ts_df.empty):
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
        ts_df = self._warehouse.get_target_share(current_season)

        if ts_df is None or (isinstance(ts_df, pd.DataFrame) and ts_df.empty):
            return {"error": "target share not available"}

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
            weekly = self._warehouse.get_seasonal_stats(season)
            if weekly is None or (isinstance(weekly, pd.DataFrame) and weekly.empty):
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
                        # Compute target share from target_share data
                        ts = self._warehouse.get_target_share(season)
                        rec_ts = 0.0
                        if not ts.empty and "avg_target_share" in ts.columns:
                            rec_rows = ts[ts["player_name"].str.contains(rec_last, case=False, na=False)]
                            if not rec_rows.empty:
                                rec_ts = float(rec_rows["avg_target_share"].iloc[0])
                        histories.append({
                            "qb": qb["name"],
                            "receiver": rec["name"],
                            "season": season,
                            "games_together": int(len(shared)),
                            "receiver_target_share": round(rec_ts, 3),
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

        # PFR team codes differ from standard NFL abbreviations for 8 teams
        _NFL_TO_PFR = {
            "GB": "GNB", "KC": "KAN", "LA": "LAR", "LV": "LVR",
            "NO": "NOR", "NE": "NWE", "SF": "SFO", "TB": "TAM",
        }
        pfr_code = _NFL_TO_PFR.get(team.upper(), team.upper())
        team_picks = picks_df[picks_df[team_col].str.upper() == pfr_code]
        if team_picks.empty:
            return []

        college_df = self._college_cache.get("college_target_share")

        result = []
        for _, pick in team_picks.iterrows():
            player_name = str(pick.get("pfr_player_name", pick.get("player_name", pick.get("player", ""))))
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
        college_df = self._college_cache.get("college_target_share")
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
            # Try exact name match first (most reliable)
            result = await session.execute(
                select(Player).where(Player.name == player_name)
            )
            player = result.scalar_one_or_none()

            if not player:
                # Fallback: last-name search with first-name disambiguation
                last = player_name.split()[-1].lower()
                first = player_name.split()[0].lower() if player_name else ""
                result = await session.execute(
                    select(Player).where(Player.name.ilike(f"%{last}%"))
                )
                candidates = result.scalars().all()
                if not candidates:
                    logger.debug("Could not find player for rookie eval: %s", player_name)
                    return
                # Prefer first+last name match to avoid cross-player confusion
                player = next(
                    (p for p in candidates if first and p.name.split()[0].lower() == first),
                    candidates[0],
                )

            from decimal import Decimal

            # Veteran guard: if player has NFL history, do NOT mark as rookie.
            # This prevents loose name matching from overwriting veteran status.
            if player.nfl_seasons_played is not None and player.nfl_seasons_played >= 1:
                logger.debug(
                    "Skipping rookie eval for %s — %d NFL seasons played (veteran)",
                    player.name, player.nfl_seasons_played,
                )
                return

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

        Uses nfl_comp_builder for draft-capital-based profile grading and
        historical comp matching. Falls back to cfb_data for college stats
        if available, but draft position is the primary signal.
        """
        player_name = pick.get("player_name", "")
        position    = pick.get("position", "")
        pick_num    = pick.get("pick_number", 200)
        draft_round = pick.get("round", 7)

        capital_value  = nfl_data.get_draft_capital_value(draft_round, pick_num)
        capital_signal = nfl_data.get_capital_signal(capital_value)

        # Profile grade from draft position (NFL already evaluated the talent)
        profile_grade = nfl_comp_builder.grade_college_profile_by_pick(pick_num)

        # Historical comps from nfl_comp_builder (nfl_data_py only — no R needed)
        nfl_comp_table = self._college_cache.get("nfl_comp_table")
        comps = nfl_comp_builder.find_comps(
            nfl_comp_table, position, pick_num, n=5,
        ) if nfl_comp_table is not None else []

        # Tier-based average PPG (reliable aggregate even when individual comps are sparse)
        tier_avgs = self._college_cache.get("nfl_comp_tier_averages") or {}
        tier_key = (position, profile_grade)
        tier_avg = tier_avgs.get(tier_key, {})

        # Use comp-level averages if available, fall back to tier averages
        yr1_ppg: float | None = None
        yr2_ppg: float | None = None
        if comps:
            yr1_vals = [c["yr1_ppg"] for c in comps if c.get("yr1_ppg") is not None]
            yr2_vals = [c["yr2_ppg"] for c in comps if c.get("yr2_ppg") is not None]
            yr1_ppg = sum(yr1_vals) / len(yr1_vals) if yr1_vals else None
            yr2_ppg = sum(yr2_vals) / len(yr2_vals) if yr2_vals else None

        # Fill gaps with tier averages
        if yr1_ppg is None:
            yr1_ppg = tier_avg.get("yr1_avg_ppg")
        if yr2_ppg is None:
            yr2_ppg = tier_avg.get("yr2_avg_ppg")

        landing_mod = self._get_landing_spot_modifier(
            team_context.get("system_grade", {})
        )

        # Try to get college stats from cfb_data if available
        college_profile = self._get_college_profile(player_name, position)
        raw_dom = college_profile.get("dominator_rating", 0.0)
        conf    = college_profile.get("conference", "Unknown")
        adj_dom = cfb_data.get_adjusted_dominator(raw_dom, conf)

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
            "projection_confidence":  "low" if profile_grade in ("weak",) else "medium",
            "variance_flag":          True,
        })

        return await self._generate_rookie_displacement_flags(
            pick, position, capital_signal, team_context
        )

    # ------------------------------------------------------------------
    # Team assignment sync — update team_abbr from transactions
    # ------------------------------------------------------------------

    async def _sync_player_teams(
        self,
        transactions: list[dict],
        team_abbr: str,
    ) -> int:
        """
        Update team_abbr for any player who changed teams.
        OTC transactions are the source of truth for current roster.
        Returns count of players updated.

        When the transaction type is empty (OTC cap table format),
        presence in the team's list is treated as an arrival — but only
        exact name matches are used to avoid cross-player confusion
        (e.g. "Julian Love" matching "Jordan Love" via last-name fallback).
        """
        ARRIVAL_KEYWORDS = {"sign", "claim", "draft", "trad"}
        DEPARTURE_KEYWORDS = {"release", "waiv", "retire", "cut"}

        updated = 0
        async with AsyncSessionLocal() as session:
            for txn in transactions:
                player_name = (txn.get("player") or "").strip()
                txn_type = (txn.get("type") or "").lower().strip()

                if not player_name:
                    continue

                # Determine new team based on transaction type
                new_team: str | None = None
                if txn_type:
                    if any(kw in txn_type for kw in ARRIVAL_KEYWORDS):
                        new_team = team_abbr.upper()
                    elif any(kw in txn_type for kw in DEPARTURE_KEYWORDS):
                        new_team = "FA"
                else:
                    # OTC cap tables have empty type — presence in team's
                    # transaction list means the player is on this team
                    new_team = team_abbr.upper()

                if not new_team:
                    continue

                # Exact name match only — no last-name fallback for team
                # syncing to avoid cross-player confusion (e.g. "Julian
                # Love" on SEA cap table matching "Jordan Love" on GB)
                result = await session.execute(
                    select(Player).where(Player.name == player_name)
                )
                player = result.scalar_one_or_none()

                if not player or player.team_abbr == new_team:
                    continue

                logger.info(
                    "Team updated: %s — %s → %s",
                    player.name, player.team_abbr, new_team,
                )
                player.team_abbr = new_team
                player.updated_at = datetime.now(timezone.utc)
                updated += 1

            if updated > 0:
                await session.commit()

        return updated

    # ------------------------------------------------------------------
    # Departure-based BENEFICIARY flags — Python-generated
    # ------------------------------------------------------------------

    async def _handle_departures(
        self,
        team_abbr: str,
        roster: list[dict],
    ) -> list[dict]:
        """
        Generate BENEFICIARY flags for incumbents when a significant starter
        departs. Same pattern as _generate_rookie_displacement_flags — pure
        Python, no API call.

        Detection: compares previous season's roster (warehouse prev_rosters) against
        current roster. Only flags departures of players with meaningful
        production (>=80 targets for WR/TE, >=150 carries for RB).
        """
        team = team_abbr.upper()
        skill_pos = {"WR", "RB", "TE"}
        current_names = {p["name"] for p in roster if p.get("position") in skill_pos}

        prev_season = get_current_season() - 1

        # Load previous roster from warehouse
        prev_rosters = self._warehouse.prev_rosters
        if prev_rosters is None or (isinstance(prev_rosters, pd.DataFrame) and prev_rosters.empty):
            return []

        team_col = next((c for c in ("team", "team_abbr") if c in prev_rosters.columns), None)
        name_col = next((c for c in ("full_name", "player_name") if c in prev_rosters.columns), None)
        if not team_col or not name_col:
            return []

        # Load target share for production filter from warehouse
        ts_df = self._warehouse.get_target_share(prev_season)

        # Build production lookup: player_id -> (targets, carries)
        prod_by_id: dict[str, tuple[int, int]] = {}
        if ts_df is not None and "player_id" in ts_df.columns:
            ts_team = "recent_team" if "recent_team" in ts_df.columns else "team"
            team_mask = ts_df[ts_team].str.upper() == team if ts_team in ts_df.columns else True
            for _, r in ts_df[team_mask].iterrows():
                pid = str(r.get("player_id", "")).strip()
                tgts = int(r.get("total_targets", 0) or 0)
                carries = int(r.get("total_carries", 0) or 0)
                if pid:
                    existing = prod_by_id.get(pid, (0, 0))
                    prod_by_id[pid] = (existing[0] + tgts, existing[1] + carries)

        # Min production thresholds — only flag WR1/RB1/TE1 departures
        MIN_TARGETS = 80   # ~5 targets/game — WR1/TE1 level
        MIN_CARRIES = 150  # ~9 carries/game — RB1 level

        mask = prev_rosters[team_col].str.upper() == team
        if "position" in prev_rosters.columns:
            mask &= prev_rosters["position"].isin(skill_pos)
        prev_df = prev_rosters[mask].drop_duplicates(subset=[name_col])

        flags: list[dict] = []
        for _, row in prev_df.iterrows():
            departed_name = str(row.get(name_col, "")).strip()
            departed_pos = str(row.get("position", "")).strip()
            if not departed_name or departed_pos not in skill_pos:
                continue
            if departed_name in current_names:
                continue  # Still on the team

            # Production filter: skip depth/practice squad players
            pid = str(row.get("player_id", "")).strip()
            tgts, carries = prod_by_id.get(pid, (0, 0))
            if departed_pos in ("WR", "TE") and tgts < MIN_TARGETS:
                continue
            if departed_pos == "RB" and carries < MIN_CARRIES:
                continue

            logger.info(
                "%s departure: %s (%s) — targets=%d, carries=%d",
                team, departed_name, departed_pos, tgts, carries,
            )

            # Only flag top-3 same-position incumbents (by production)
            same_pos = [
                inc for inc in roster
                if inc.get("position") == departed_pos
                and inc.get("name") != departed_name
            ]
            # Sort incumbents by their production (descending)
            for inc in same_pos:
                inc_name = inc.get("name", "")
                # Find incumbent's player_id from current roster
                inc_pid = ""
                if "player_id" in prev_rosters.columns:
                    inc_row = prev_rosters[prev_rosters[name_col] == inc_name]
                    if not inc_row.empty:
                        inc_pid = str(inc_row.iloc[0].get("player_id", ""))
                inc_tgts, inc_carries = prod_by_id.get(inc_pid, (0, 0))
                inc["_prod_sort"] = inc_tgts + inc_carries
            same_pos.sort(key=lambda x: x.get("_prod_sort", 0), reverse=True)

            for inc in same_pos[:3]:  # top-3 beneficiaries only
                flags.append({
                    "player_name": inc["name"],
                    "player_team": team,
                    "player_position": departed_pos,
                    "flag_type": "beneficiary",
                    "trigger_player_name": departed_name,
                    "trigger_player_team": team,
                    "trigger_condition": "departed_team",
                    "effect_on_value": "positive",
                    "value_impact_pct": 0.35 if departed_pos == "WR" else 0.25,
                    "confidence": "high",
                    "reasoning": (
                        f"{departed_name} departed {team} — "
                        f"{inc['name']} inherits {departed_pos} role share"
                    ),
                    "season_year": get_analysis_year(),
                })

        if flags:
            logger.info(
                "%s: %d departure BENEFICIARY flags generated",
                team_abbr, len(flags),
            )
        return flags

    # ------------------------------------------------------------------
    # Arrival-based DISPLACED + CONTINGENT flags — Python-generated
    # ------------------------------------------------------------------

    async def _handle_arrivals(
        self,
        team_abbr: str,
        roster: list[dict],
    ) -> list[dict]:
        """
        Generate DISPLACED + CONTINGENT flags for incumbents when a significant
        player arrives on the team. Mirror of _handle_departures().

        Detection: compares current roster against previous season's roster
        (warehouse prev_rosters) for this team. Players currently on the roster
        who were NOT on this team last season are arrivals. Only flags arrivals
        with meaningful prior production (>=80 targets for WR/TE, >=150 carries for RB).
        """
        team = team_abbr.upper()
        skill_pos = {"WR", "RB", "TE"}

        # Current skill position players from OTC roster
        current_skill = [p for p in roster if p.get("position") in skill_pos]
        current_names = {p["name"] for p in current_skill}

        prev_season = get_current_season() - 1

        # Load previous roster from warehouse
        prev_rosters = self._warehouse.prev_rosters
        if prev_rosters is None or (isinstance(prev_rosters, pd.DataFrame) and prev_rosters.empty):
            return []

        team_col = next(
            (c for c in ("team", "team_abbr") if c in prev_rosters.columns), None
        )
        name_col = next(
            (c for c in ("full_name", "player_name") if c in prev_rosters.columns), None
        )
        if not team_col or not name_col:
            return []

        # Players who were on THIS team last season
        prev_team_mask = prev_rosters[team_col].str.upper() == team
        prev_team_names = set(prev_rosters[prev_team_mask][name_col].dropna().unique())

        # Arrivals = on current roster but NOT on this team last season
        arrival_names = current_names - prev_team_names
        if not arrival_names:
            return []

        # Load target share for production filter from warehouse (ALL teams — arrival was elsewhere)
        ts_df = self._warehouse.get_target_share(prev_season)

        # Build production lookup by player_id
        prod_by_id: dict[str, tuple[int, int]] = {}
        if ts_df is not None and "player_id" in ts_df.columns:
            for _, r in ts_df.iterrows():
                pid = str(r.get("player_id", "")).strip()
                tgts = int(r.get("total_targets", 0) or 0)
                carries = int(r.get("total_carries", 0) or 0)
                if pid:
                    existing = prod_by_id.get(pid, (0, 0))
                    prod_by_id[pid] = (
                        max(existing[0], tgts),
                        max(existing[1], carries),
                    )

        MIN_TARGETS = 80   # ~5 targets/game — WR1/TE1 level
        MIN_CARRIES = 150  # ~9 carries/game — RB1 level

        flags: list[dict] = []
        for arrival_name in arrival_names:
            arrival_pos = next(
                (p["position"] for p in current_skill if p["name"] == arrival_name),
                None,
            )
            if not arrival_pos or arrival_pos not in skill_pos:
                continue

            # Find arrival's player_id from previous roster (any team)
            arrival_mask = prev_rosters[name_col] == arrival_name
            arrival_rows = prev_rosters[arrival_mask]
            if arrival_rows.empty:
                # Last-name fallback — only if unique player_id
                last = arrival_name.split()[-1] if arrival_name else ""
                if not last:
                    continue
                arrival_mask = prev_rosters[name_col].str.contains(
                    last, case=False, na=False
                )
                arrival_rows = prev_rosters[arrival_mask]
                if "player_id" in arrival_rows.columns:
                    if arrival_rows["player_id"].nunique() != 1:
                        continue  # Ambiguous — skip

            if arrival_rows.empty:
                continue

            arrival_pid = str(
                arrival_rows.iloc[0].get("player_id", "")
            ).strip()
            tgts, carries = prod_by_id.get(arrival_pid, (0, 0))

            # Production threshold — only flag significant arrivals
            if arrival_pos in ("WR", "TE") and tgts < MIN_TARGETS:
                continue
            if arrival_pos == "RB" and carries < MIN_CARRIES:
                continue

            logger.info(
                "%s arrival: %s (%s) — targets=%d, carries=%d",
                team, arrival_name, arrival_pos, tgts, carries,
            )

            # Look up arrival's depth rank from depth chart
            arrival_depth_rank = None
            if hasattr(self._warehouse, "get_player_depth_rank") and arrival_pid:
                arrival_depth_rank = self._warehouse.get_player_depth_rank(arrival_pid)

            # Generate displaced + contingent for same-position incumbents
            same_pos = [
                inc for inc in current_skill
                if inc.get("position") == arrival_pos
                and inc.get("name") != arrival_name
            ]

            impact_pct = -0.30 if arrival_pos in ("WR", "TE") else -0.25

            for inc in same_pos:
                inc_name = inc.get("name", "")

                # Skip deep depth chart noise: don't flag rank 3+ incumbents
                if hasattr(self._warehouse, "get_player_depth_rank"):
                    inc_rows_prev = prev_rosters[prev_rosters[name_col] == inc_name]
                    if not inc_rows_prev.empty and "player_id" in inc_rows_prev.columns:
                        inc_gsis = str(inc_rows_prev.iloc[0].get("player_id", "")).strip()
                        if inc_gsis:
                            inc_rank = self._warehouse.get_player_depth_rank(inc_gsis)
                            if inc_rank is not None and inc_rank >= 3:
                                continue

                # Set confidence based on arrival depth rank
                if arrival_depth_rank == 1:
                    confidence = "high"
                elif arrival_depth_rank == 2:
                    confidence = "medium"
                else:
                    confidence = "high"  # default for significant arrivals without DC data

                flags.append({
                    "player_name": inc_name,
                    "player_team": team,
                    "player_position": arrival_pos,
                    "flag_type": "displaced",
                    "trigger_player_name": arrival_name,
                    "trigger_player_team": team,
                    "trigger_condition": "active_and_healthy",
                    "effect_on_value": "negative",
                    "value_impact_pct": impact_pct,
                    "confidence": confidence,
                    "reasoning": (
                        f"{arrival_name} arrived on {team} with "
                        f"{tgts} targets / {carries} carries last season "
                        f"— {inc_name} target share threatened."
                    ),
                    "season_year": get_analysis_year(),
                })
                flags.append({
                    "player_name": inc_name,
                    "player_team": team,
                    "player_position": arrival_pos,
                    "flag_type": "contingent",
                    "trigger_player_name": arrival_name,
                    "trigger_player_team": team,
                    "trigger_condition": "injured_or_absent",
                    "effect_on_value": "positive",
                    "value_impact_pct": abs(impact_pct) * 0.8,
                    "confidence": confidence,
                    "reasoning": (
                        f"{inc_name} value recovers if "
                        f"{arrival_name} misses time."
                    ),
                    "season_year": get_analysis_year(),
                })

        if flags:
            logger.info(
                "%s: %d arrival DISPLACED/CONTINGENT flags generated",
                team_abbr, len(flags),
            )
        return flags

    # ------------------------------------------------------------------
    # Per-team runner — exactly ONE call_once()
    # ------------------------------------------------------------------

    async def run_for_team(self, team_abbr: str) -> list[dict]:
        """Run for one team. One Sonnet call. Returns list of flag dicts."""
        if self._warehouse is None:
            from backend.integrations.nfl_data import NflDataWarehouse
            self._warehouse = NflDataWarehouse.build()
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
            comp_table = self._college_cache.get("historical_comp_table")
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

            # Arrival-based DISPLACED + CONTINGENT flags — Python-generated
            try:
                arrival_flags = await self._handle_arrivals(
                    team_abbr,
                    context.get("current_roster", []),
                )
                if arrival_flags:
                    flags.extend(arrival_flags)
            except Exception as exc:
                logger.warning("Arrival handler failed for %s: %s", team_abbr, exc)

            # Dedup + enforce mutual exclusivity before DB write
            flags = deduplicate_flags(flags)
            flags = downgrade_specialist_committee_flags(
                flags, context.get("backfield_usage"),
            )
            flags = enforce_flag_mutual_exclusivity(flags)

            written = await _write_flags(flags)

            # Departure-based BENEFICIARY flags — Python-generated
            try:
                departure_flags = await self._handle_departures(
                    team_abbr,
                    context.get("current_roster", []),
                )
                if departure_flags:
                    written += await _write_flags(departure_flags)
                    flags.extend(departure_flags)
            except Exception as exc:
                logger.warning("Departure handler failed for %s: %s", team_abbr, exc)

            # Sync player team assignments from transactions
            try:
                synced = await self._sync_player_teams(
                    context.get("transactions", []), team_abbr
                )
                if synced:
                    logger.info(
                        "%s: %d player team(s) synced", team_abbr, synced
                    )
            except Exception as exc:
                logger.warning("Team sync failed for %s: %s", team_abbr, exc)

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

    async def run_all_teams(self, warehouse=None, concurrency: int = 2) -> dict[str, int]:
        """
        Run all 32 teams. NFL data from warehouse, college/comp data loaded here.
        Returns {team_abbr: flag_count}.
        """
        from backend.agents.team_systems import NFL_TEAMS

        if warehouse is not None:
            self._warehouse = warehouse

        current_season = get_current_season()
        analysis_year  = get_analysis_year()

        # Pre-load OTC transactions once for all teams
        logger.info("Pre-loading OTC transactions for %d...", analysis_year)
        await overthecap.preload_transactions([analysis_year])

        # Pre-load college data for draft pick evaluation (optional — may fail without R)
        try:
            college_seasons = list(range(current_season - 6, current_season))
            logger.info("Pre-loading college target share for seasons %s...", college_seasons)
            college_df = cfb_data.get_college_target_share(college_seasons)
            self._college_cache["college_target_share"] = college_df
        except Exception as exc:
            logger.warning("Could not pre-load college data (R not installed — using draft capital only): %s", exc)

        # Pre-load NFL comp table (nfl_data_py only — no R/cfbfastR needed)
        try:
            logger.info("Building NFL comp table from historical draft data...")
            nfl_comp_table = nfl_comp_builder.build_comp_table()
            self._college_cache["nfl_comp_table"] = nfl_comp_table
            tier_avgs = nfl_comp_builder.get_tier_averages(nfl_comp_table)
            self._college_cache["nfl_comp_tier_averages"] = tier_avgs
            logger.info("NFL comp table: %d records, %d tier averages", len(nfl_comp_table), len(tier_avgs))
        except Exception as exc:
            logger.warning("Could not build NFL comp table: %s", exc)

        # Legacy cfb_data comp table (kept for backwards compat if R is installed)
        try:
            comp_table = cfb_data.build_historical_comp_table()
            self._college_cache["historical_comp_table"] = comp_table
            logger.info("Historical comp table (cfb): %d records", len(comp_table))
        except Exception as exc:
            logger.debug("cfb comp table not available (expected without R): %s", exc)

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
) -> dict[tuple, tuple[str | None, str | None]]:
    """
    Resolve all player names in ONE query.
    Returns {(name, team): (player_id, resolved_team)}.
    """
    results: dict[tuple, tuple[str | None, str | None]] = {}
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
            results[(name, team)] = (None, None)
            continue
        last = name.split()[-1].lower()
        candidates = player_map.get(last, [])
        if not candidates:
            results[(name, team)] = (None, None)
        elif len(candidates) == 1:
            results[(name, team)] = (str(candidates[0].id), candidates[0].team_abbr)
        else:
            # Prefer exact full-name match
            exact = [p for p in candidates if p.name.lower() == name.lower()]
            if len(exact) == 1:
                results[(name, team)] = (str(exact[0].id), exact[0].team_abbr)
                continue
            # Then filter by team
            if team:
                match = [p for p in (exact or candidates) if p.team_abbr == team.upper()]
                if match:
                    results[(name, team)] = (str(match[0].id), match[0].team_abbr)
                    continue
            # Last resort: first candidate (but flag team for cross-team validation)
            pick = (exact or candidates)[0]
            results[(name, team)] = (str(pick.id), pick.team_abbr)

    return results


async def _write_flags(flags: list[dict]) -> int:
    """Replace all dependency flags for the affected players in one DB transaction.

    Delete-then-insert ensures re-runs (including cache hits) are idempotent.
    All flags in one batch belong to the same team, so we delete by resolved
    player IDs + season_year before inserting the fresh set.
    """
    if not flags:
        return 0

    # Reject flags missing required fields before DB write
    valid_flags = [f for f in flags if validate_flag(f)]
    invalid_count = len(flags) - len(valid_flags)
    if invalid_count > 0:
        logger.warning("Rejected %d invalid flags before DB write", invalid_count)
    flags = valid_flags
    if not flags:
        return 0

    from sqlalchemy import delete as sa_delete
    analysis_year = get_analysis_year()

    # Flag types that REQUIRE player and trigger on the same team.
    # beneficiary with trigger_condition="departed_team" is exempt
    # (the trigger left the team, so they're on a different team now).
    SAME_TEAM_FLAG_TYPES = {"displaced", "contingent", "committee", "scheme_fit", "college_trust"}

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
            pid, _ = id_map.get((flag.get("player_name", ""), flag.get("player_team")), (None, None))
            if pid:
                player_ids_in_batch.add(pid)

        # Delete ALL existing flags for these players to prevent duplicates on re-run.
        # Previously scoped to season_year == analysis_year, but the model sometimes
        # outputs flags with different season_year values (e.g. 2027), leaving stale
        # duplicates behind. Deleting by player_id alone ensures a clean slate.
        if player_ids_in_batch:
            await session.execute(
                sa_delete(PlayerDependency).where(
                    PlayerDependency.player_id.in_(player_ids_in_batch),
                )
            )

        written = 0
        cross_team_rejected = 0
        for flag in flags:
            player_name  = flag.get("player_name", "")
            player_team  = flag.get("player_team")
            trigger_name = flag.get("trigger_player_name", "")
            trigger_team = flag.get("trigger_player_team")

            player_id, player_resolved_team = id_map.get(
                (player_name, player_team), (None, None)
            )
            trigger_id, trigger_resolved_team = id_map.get(
                (trigger_name, trigger_team), (None, None)
            )

            if not player_id:
                logger.debug("Could not resolve player: %s (%s)", player_name, player_team)
                continue

            # Cross-team validation: reject flags where player and trigger
            # resolved to different teams (phantom flags from model errors).
            flag_type = flag.get("flag_type", "")
            trigger_condition = flag.get("trigger_condition", "")
            if (
                trigger_id
                and player_resolved_team
                and trigger_resolved_team
                and player_resolved_team != trigger_resolved_team
                and flag_type in SAME_TEAM_FLAG_TYPES
            ):
                cross_team_rejected += 1
                logger.debug(
                    "Rejected cross-team %s: %s (%s) <- %s (%s)",
                    flag_type, player_name, player_resolved_team,
                    trigger_name, trigger_resolved_team,
                )
                continue
            # Also reject beneficiary flags where trigger is on a different
            # team BUT the trigger_condition is NOT "departed_team"
            if (
                flag_type == "beneficiary"
                and trigger_condition != "departed_team"
                and trigger_id
                and player_resolved_team
                and trigger_resolved_team
                and player_resolved_team != trigger_resolved_team
            ):
                cross_team_rejected += 1
                logger.debug(
                    "Rejected cross-team beneficiary: %s (%s) <- %s (%s)",
                    player_name, player_resolved_team,
                    trigger_name, trigger_resolved_team,
                )
                continue

            session.add(PlayerDependency(
                player_id=player_id,
                flag_type=flag_type,
                trigger_player_id=trigger_id,
                trigger_player_name=trigger_name,
                trigger_condition=trigger_condition or "active_and_healthy",
                effect_on_value=flag.get("effect_on_value", ""),
                value_impact_pct=flag.get("value_impact_pct"),
                confidence=flag.get("confidence", "medium"),
                reasoning=flag.get("reasoning", ""),
                season_year=flag.get("season_year", analysis_year),
            ))
            written += 1

        if cross_team_rejected:
            logger.info("Rejected %d cross-team phantom flags", cross_team_rejected)

        await session.commit()

    return written


# ---------------------------------------------------------------------------
# Module-level compatibility shims (used by pipeline.py and scripts)
# ---------------------------------------------------------------------------

_agent_instance: RosterChangesAgent | None = None


def _get_agent(dry_run: bool = False, warehouse=None) -> RosterChangesAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = RosterChangesAgent(dry_run=dry_run, warehouse=warehouse)
    elif warehouse is not None:
        _agent_instance._warehouse = warehouse
    return _agent_instance


async def run_for_team(team_abbr: str, dry_run: bool = False) -> list[dict]:
    return await _get_agent(dry_run).run_for_team(team_abbr)


async def run_all_teams(
    concurrency: int = 4, dry_run: bool = False, warehouse=None,
) -> dict[str, int]:
    return await _get_agent(dry_run, warehouse=warehouse).run_all_teams(
        warehouse=warehouse, concurrency=concurrency,
    )
