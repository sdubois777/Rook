"""
Agent 4: Injury Risk Agent

Pre-computes all injury pattern flags in Python (deterministic, testable).
One Haiku call per team synthesizes flags into risk level and value modifier.

Architecture:
  - Model: Haiku (classification)
  - Max tokens: 1000 per team batch
  - Pattern: pre-aggregate in Python → ONE call_once() per team → parse JSON → write DB
  - Never uses run_agent() (that is for live draft only)

Key outputs per player:
  - overall_risk_level (low/moderate/high/volatile)
  - risk_adjusted_value_modifier (-0.35 to 0.0)
  - pattern_flags (RECURRING_SOFT_TISSUE, CONCUSSION_HISTORY, HIGH_MILEAGE, etc.)
  - injury_log, chronic_conditions, recovery_assessment
"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal

import pandas as pd
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU
from backend.agents.team_systems import NFL_TEAMS
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerInjuryProfile
from backend.utils.seasons import get_current_season, get_analysis_seasons, get_analysis_year

logger = logging.getLogger(__name__)

SKILL_POSITIONS = {"WR", "RB", "TE"}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a fantasy football injury risk analyst building a pre-draft research database.

You receive pre-aggregated injury data for every skill-position player on one NFL team.
Injury history, pattern flags, and age risk multipliers are pre-computed in Python.
Your job: synthesize the flags into an overall risk level and value modifier.

Each object must match this schema exactly:
{
  "player_name": "string",
  "overall_risk_level": "string (low/moderate/high/volatile)",
  "risk_adjusted_value_modifier": float (0.0 to -0.50, negative means risk penalty),
  "recovery_assessment": "string (probable/questionable/doubtful) or null",
  "risk_notes": "1-2 sentence summary of the key risk factors"
}

Risk level and modifier guidelines (apply age_risk_mult as a boost):
  low:      0.0 to -0.05  — clean history, no pattern flags
  moderate: -0.10 to -0.20 — 1 non-severe flag or mild age concern
  high:     -0.20 to -0.35 — multiple flags or severe injury type
  volatile: -0.35 to -0.40 max — ONLY when BOTH conditions are true:
    a) Multiple severe flags (would be HIGH on flags alone), AND
    b) Player missed 8+ games in 2 or more of the last 3 seasons (chronic availability problem)
    If a player had injuries concentrated in a single season only → cap at HIGH, not VOLATILE.
    One bad season does not make a player volatile — volatile means chronic unavailability.

Age risk multiplier is pre-computed (age_risk_mult field):
  Use it as a multiplier on the base modifier.
  Example: base -0.10 × 1.25x age_risk_mult → modifier = -0.125.

Pattern flag logic:
  - No flags + age < 26 → low
  - RECURRING_SOFT_TISSUE alone → moderate
  - CONCUSSION_HISTORY alone → moderate
  - HIGH_MILEAGE → moderate (elevated by age)
  - POST_ACL → high (within 18 months, regardless of other flags)
  - CHRONIC_CONDITION → moderate (does not reset)
  - WORKLOAD_CLIFF → moderate (recovery risk next season)
  - Multiple severe flags → high or volatile

recovery_assessment: set to "probable" unless recent injury suggests ongoing concern.
  Use "questionable" if they missed 4+ games last season with a lingering issue.
  Use "doubtful" only for POST_ACL or unresolved chronic.

Output ONLY a valid JSON array. No explanation, no preamble, no markdown fences.
Your entire response must be parseable by json.loads()."""


# ---------------------------------------------------------------------------
# Injury classification helpers — pure functions, fully testable
# ---------------------------------------------------------------------------

# Ordered: check most specific patterns first
_CONCUSSION_KW    = ("concussion", "head")
_STRESS_KW        = ("stress fracture", "stress reaction")
_FRACTURE_KW      = ("fracture", "broken", "fibula", "tibia", "collarbone", "clavicle")
_ACL_KEYWORDS     = ("acl", "anterior cruciate")
_ANKLE_KEYWORDS   = ("ankle",)
_SOFT_TISSUE_AREAS = {
    "hamstring": "hamstring",
    "groin":     "groin",
    "calf":      "calf",
    "hip flexor": "hip_flexor",
    "hip":       "hip_flexor",
    "quad":      "quad",
    "quadricep": "quad",
    "adductor":  "adductor",
    "thigh":     "quad",
}
_SOFT_TISSUE_KW   = tuple(_SOFT_TISSUE_AREAS.keys())
_CHRONIC_KW       = ("turf toe", "plantar", "back", "shoulder", "arthritis")


def classify_injury(injury_text: str | None) -> str:
    """
    Map an injury report text string to a risk category.

    Returns one of:
      soft_tissue | ligament_acl | high_ankle_sprain | fracture_traumatic |
      fracture_stress | concussion | chronic | other
    """
    if not injury_text:
        return "other"
    t = injury_text.lower().strip()

    if any(k in t for k in _CONCUSSION_KW):
        return "concussion"
    if any(k in t for k in _STRESS_KW):
        return "fracture_stress"
    if any(k in t for k in _FRACTURE_KW):
        return "fracture_traumatic"
    if any(k in t for k in _ACL_KEYWORDS):
        return "ligament_acl"
    if any(k in t for k in _ANKLE_KEYWORDS):
        return "high_ankle_sprain"
    if any(k in t for k in _SOFT_TISSUE_KW):
        return "soft_tissue"
    if any(k in t for k in _CHRONIC_KW):
        return "chronic"
    if "knee" in t:
        # Non-ACL knee injury — treat as soft tissue
        return "soft_tissue"
    return "other"


def get_soft_tissue_area(injury_text: str | None) -> str:
    """Return the specific body part for soft tissue injuries."""
    if not injury_text:
        return "general"
    t = injury_text.lower()
    for keyword, area in _SOFT_TISSUE_AREAS.items():
        if keyword in t:
            return area
    if "knee" in t:
        return "knee"
    return "general"


def compute_age_multiplier(age: int | None) -> float:
    """
    Return the age risk multiplier for a given player age.

    Under 26: 1.0x | 26-28: 1.1x | 29-30: 1.25x | 31+: 1.5x
    """
    if age is None:
        return 1.0
    if age < 26:
        return 1.0
    if age <= 28:
        return 1.1
    if age <= 30:
        return 1.25
    return 1.5


def compute_pattern_flags(
    injury_seasons: list[dict],
    position: str,
    age: int | None,
) -> dict:
    """
    Pre-compute all deterministic pattern flags from injury history.

    Args:
        injury_seasons: list of {season, injuries: [{category, area}], games_missed, carries}
        position:       player position string (RB, WR, TE, etc.)
        age:            player age (integer or None)

    Returns dict with:
        RECURRING_SOFT_TISSUE, CONCUSSION_HISTORY, HIGH_MILEAGE,
        POST_ACL, CHRONIC_CONDITION, WORKLOAD_CLIFF  (all bool)
        concussion_count, career_carries, last_season_carries  (int helpers)
    """
    concussion_count: int = 0
    soft_tissue_by_area: dict[str, list[int]] = {}   # area → list of seasons it appeared
    chronic_found:   bool = False
    acl_seasons:     list[int] = []
    career_carries:  int = 0
    last_season_carries: int = 0

    sorted_seasons = sorted(injury_seasons, key=lambda s: s.get("season", 0), reverse=True)

    for s in injury_seasons:
        season_num = s.get("season", 0)
        carries    = s.get("carries", 0) or 0
        career_carries += carries

        for inj in s.get("injuries", []):
            cat  = inj.get("category", "other")
            area = inj.get("area", "general")

            if cat == "concussion":
                concussion_count += 1
            elif cat == "soft_tissue":
                soft_tissue_by_area.setdefault(area, [])
                if season_num not in soft_tissue_by_area[area]:
                    soft_tissue_by_area[area].append(season_num)
            elif cat == "chronic":
                chronic_found = True
            elif cat == "ligament_acl":
                if season_num not in acl_seasons:
                    acl_seasons.append(season_num)

    if sorted_seasons:
        last_season_carries = sorted_seasons[0].get("carries", 0) or 0

    # RECURRING_SOFT_TISSUE: same body area in 2+ different seasons
    recurring_soft_tissue = any(
        len(seasons_list) >= 2
        for seasons_list in soft_tissue_by_area.values()
    )

    # POST_ACL: ACL injury within ~2 seasons (covers 18-month recovery window).
    # In May 2026 with current_season=2025, this catches 2023+ ACLs.
    current = get_current_season()
    post_acl = any(s >= current - 2 for s in acl_seasons)

    pos_upper = (position or "").upper()
    is_rb = pos_upper in {"RB", "HB", "FB"}

    return {
        "RECURRING_SOFT_TISSUE": recurring_soft_tissue,
        "CONCUSSION_HISTORY":    concussion_count >= 2,
        "HIGH_MILEAGE":          is_rb and career_carries >= 600,
        "POST_ACL":              post_acl,
        "CHRONIC_CONDITION":     chronic_found,
        "WORKLOAD_CLIFF":        is_rb and last_season_carries >= 300,
        # numeric helpers for downstream use
        "concussion_count":      concussion_count,
        "career_carries":        career_carries,
        "last_season_carries":   last_season_carries,
    }


# ---------------------------------------------------------------------------
# InjuryRiskAgent
# ---------------------------------------------------------------------------

class InjuryRiskAgent(BaseAgent):
    AGENT_NAME       = "injury_risk"
    AGENT_MODEL      = HAIKU
    AGENT_MAX_TOKENS = 1000

    # ------------------------------------------------------------------
    # Sync data helpers — read from warehouse (no network calls)
    # ------------------------------------------------------------------

    def _get_team_roster(self, team: str, season: int) -> list[dict]:
        """Return skill-position players on this team from warehouse roster data."""
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
            result.append(entry)

        return result

    def _get_player_injury_season(
        self, player_name: str, team: str, season: int
    ) -> dict:
        """
        Extract injury events and games missed for one player in one season.
        Returns {season, injuries: [{injury_text, category, area}], games_missed}.
        """
        inj_df = self._warehouse.get_injuries(season)
        if inj_df is None or inj_df.empty:
            return {"season": season, "injuries": [], "games_missed": 0}

        # Filter to regular season
        if "game_type" in inj_df.columns:
            df = inj_df[inj_df["game_type"] == "REG"].copy()
        else:
            df = inj_df.copy()

        if df.empty:
            return {"season": season, "injuries": [], "games_missed": 0}

        # Match player by last name + team
        last     = player_name.split()[-1]
        name_col = next((c for c in ("full_name", "player_name") if c in df.columns), None)
        team_col = next((c for c in ("team", "team_abbr") if c in df.columns), None)
        if not name_col:
            return {"season": season, "injuries": [], "games_missed": 0}

        mask = df[name_col].str.contains(last, case=False, na=False)
        if team_col:
            team_mask = mask & (df[team_col].str.upper() == team.upper())
            player_df = df[team_mask]
            # Fallback: if no injuries found under current team, try any team.
            # This handles traded players (e.g. Chubb CLE→HOU) whose injuries
            # were recorded under their previous team.
            if player_df.empty:
                player_df = df[mask]
        else:
            player_df = df[mask]

        if player_df.empty:
            return {"season": season, "injuries": [], "games_missed": 0}

        # Collect unique injuries (deduplicate by category+area)
        injuries: list[dict] = []
        seen:     set  = set()
        for _, row in player_df.iterrows():
            primary = row.get("report_primary_injury")
            if not primary or pd.isna(primary):
                continue
            text = str(primary)
            cat  = classify_injury(text)
            area = get_soft_tissue_area(text) if cat == "soft_tissue" else cat
            key  = (cat, area)
            if key not in seen:
                seen.add(key)
                injuries.append({
                    "injury_text": text,
                    "category":    cat,
                    "area":        area,
                })

        # Count weeks marked "Out" (proxy for games missed)
        games_missed = 0
        if "report_status" in player_df.columns:
            games_missed = int(
                player_df["report_status"]
                .str.lower()
                .str.strip()
                .eq("out")
                .sum()
            )

        return {
            "season":       season,
            "injuries":     injuries,
            "games_missed": games_missed,
        }

    def _get_player_carries(self, player_name: str, team: str, season: int) -> int:
        """Return total carries for one player in one season from the carries cache."""
        carry_df = self._warehouse.get_target_share(season)
        if carry_df is None or carry_df.empty:
            return 0

        last     = player_name.split()[-1]
        name_col = next((c for c in ("player_name", "full_name") if c in carry_df.columns), None)
        team_col = next((c for c in ("recent_team", "team") if c in carry_df.columns), None)
        if not name_col:
            return 0

        mask = carry_df[name_col].str.contains(last, case=False, na=False)
        if team_col:
            mask = mask & (carry_df[team_col].str.upper() == team.upper())
        rows = carry_df[mask]
        if rows.empty:
            return 0

        v = rows.iloc[0].get("total_carries", 0)
        try:
            return int(v) if v is not None and pd.notna(v) else 0
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # Context builder — all Python, zero API calls
    # ------------------------------------------------------------------

    async def _build_team_context(self, team_abbr: str) -> dict:
        team             = team_abbr.upper()
        current_season   = get_current_season()
        analysis_seasons = get_analysis_seasons(3)
        analysis_year    = get_analysis_year()

        roster = self._get_team_roster(team, current_season)
        seen:    set[str]   = set()
        players: list[dict] = []

        for info in roster:
            pname = info["name"]
            if pname in seen:
                continue
            seen.add(pname)

            pos = info.get("position", "")
            age = info.get("age")

            # Collect injury history per analysis season
            injury_seasons: list[dict] = []
            for season in analysis_seasons:
                season_inj         = self._get_player_injury_season(pname, team, season)
                carries            = self._get_player_carries(pname, team, season)
                season_inj["carries"] = carries
                injury_seasons.append(season_inj)

            # Pre-compute pattern flags (deterministic)
            flags      = compute_pattern_flags(injury_seasons, pos, age)
            mult       = compute_age_multiplier(age)
            active_flags = [
                k for k in (
                    "RECURRING_SOFT_TISSUE", "CONCUSSION_HISTORY", "HIGH_MILEAGE",
                    "POST_ACL", "CHRONIC_CONDITION", "WORKLOAD_CLIFF"
                )
                if flags.get(k, False)
            ]

            players.append({
                "name":               pname,
                "position":           pos,
                "age":                age,
                "age_risk_mult":      mult,
                "pattern_flags":      active_flags,
                "concussion_count":   flags["concussion_count"],
                "career_carries":     flags["career_carries"],
                "last_season_carries": flags["last_season_carries"],
                "injury_seasons":     injury_seasons,
            })

        return {
            "team":          team,
            "analysis_year": analysis_year,
            "players":       players,
        }

    # ------------------------------------------------------------------
    # Per-team runner — exactly ONE call_once()
    # ------------------------------------------------------------------

    async def run_for_team(self, team_abbr: str) -> int:
        """Run for one team. Returns number of injury profile records written."""
        if self._warehouse is None:
            from backend.integrations.nfl_data import NflDataWarehouse
            self._warehouse = NflDataWarehouse.build()

        team = team_abbr.upper()
        logger.info("Building injury risk context for %s", team)

        try:
            context = await self._build_team_context(team)

            if not context["players"]:
                logger.info("%s: no skill-position players, skipping", team)
                return 0

            raw = await self.call_once(
                system=SYSTEM_PROMPT,
                user=(
                    f"Assess injury risk for the {team} skill-position players "
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

            written = await _write_injury_profiles(profiles, context, team)
            logger.info("%s: %d injury profiles written", team, written)
            return written

        except Exception as exc:
            logger.error("Injury Risk Agent failed for %s: %s", team, exc, exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Full pipeline — pre-warm caches once, then run all 32 teams
    # ------------------------------------------------------------------

    async def run_all_teams(
        self, warehouse=None, concurrency: int = 4
    ) -> dict[str, int]:
        """
        Reads all data from the warehouse — no independent data fetching.
        Returns {team_abbr: profiles_written}.
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
        logger.info("Injury Risk pipeline complete: %d total profiles written", total)
        return results


# ---------------------------------------------------------------------------
# Bulk DB write helpers
# ---------------------------------------------------------------------------

async def _bulk_resolve_player_ids(
    session: AsyncSession,
    names_and_teams: list[tuple[str, str]],
) -> dict[tuple, str | None]:
    """Resolve player IDs from (name, team) pairs in a single query.

    Uses team-based fetch for efficiency and builds gsis_id lookup map
    alongside name-based matching for future gsis_id-first support.
    """
    results: dict[tuple, str | None] = {}
    if not names_and_teams:
        return results

    unique_teams = {t for _, t in names_and_teams if t}
    if not unique_teams:
        return results

    # Fetch all players for relevant teams (single efficient query)
    team_conditions = [Player.team_abbr == t for t in unique_teams]
    all_players = (
        await session.execute(select(Player).where(or_(*team_conditions)))
    ).scalars().all()

    # Build lookup maps
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


async def _write_injury_profiles(
    profiles: list[dict], context: dict, team: str
) -> int:
    """Bulk upsert player_injury_profiles — one DB transaction per team."""
    if not profiles:
        return 0

    ctx_map: dict[str, dict] = {p["name"]: p for p in context.get("players", [])}

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

            ctx_player   = ctx_map.get(pname, {})
            pat_flags    = ctx_player.get("pattern_flags", [])
            inj_seasons  = ctx_player.get("injury_seasons", [])

            # Build structured injury_log
            injury_log: list[dict] = []
            chronic_list: list[str] = []
            for s in inj_seasons:
                for inj in s.get("injuries", []):
                    injury_log.append({
                        "year":         s.get("season"),
                        "injury_type":  inj.get("injury_text"),
                        "category":     inj.get("category"),
                        "games_missed": s.get("games_missed", 0),
                    })
                    if inj.get("category") == "chronic":
                        chronic_list.append(inj.get("injury_text", ""))

            # Upsert PlayerInjuryProfile
            existing = (await session.execute(
                select(PlayerInjuryProfile).where(
                    PlayerInjuryProfile.player_id == player_id
                )
            )).scalar_one_or_none()

            if existing:
                record = existing
            else:
                record = PlayerInjuryProfile(player_id=player_id)
                session.add(record)

            # Enforce VOLATILE requires multi-season injury history:
            # Must have missed 8+ games in 2+ of last 3 seasons.
            # Single bad season caps at HIGH.
            risk_level = prof.get("overall_risk_level", "low")
            risk_mod = prof.get("risk_adjusted_value_modifier", 0)
            if risk_level == "volatile":
                recent = sorted(inj_seasons, key=lambda s: s.get("season", 0), reverse=True)[:3]
                severe_seasons = sum(1 for s in recent if s.get("games_missed", 0) >= 8)
                if severe_seasons < 2:
                    logger.info(
                        "Downgrading %s from volatile to high: "
                        "only %d of last 3 seasons with 8+ games missed",
                        pname, severe_seasons,
                    )
                    risk_level = "high"
                    # Cap modifier at high range max
                    if risk_mod is not None and risk_mod < -0.35:
                        risk_mod = -0.35

            record.overall_risk_level          = risk_level
            record.risk_adjusted_value_modifier = _to_decimal(risk_mod)
            record.injury_log                  = injury_log
            record.pattern_flags               = pat_flags
            record.chronic_conditions          = chronic_list
            record.career_carry_count          = ctx_player.get("career_carries", 0)
            record.workload_cliff_flag         = "WORKLOAD_CLIFF" in pat_flags
            record.high_mileage_flag           = "HIGH_MILEAGE" in pat_flags
            record.post_acl_flag               = "POST_ACL" in pat_flags
            record.concussion_count            = ctx_player.get("concussion_count", 0)
            record.recovery_assessment         = prof.get("recovery_assessment")
            record.age_risk_multiplier         = _to_decimal(ctx_player.get("age_risk_mult"))
            record.risk_notes                  = prof.get("risk_notes")

            # Update players.risk_adjusted_value when baseline is available
            modifier = prof.get("risk_adjusted_value_modifier")
            if modifier is not None:
                player_row = (await session.execute(
                    select(Player).where(Player.id == player_id)
                )).scalar_one_or_none()
                if player_row and player_row.baseline_value is not None:
                    adj = float(player_row.baseline_value) * (1.0 + float(modifier))
                    player_row.risk_adjusted_value = Decimal(str(round(adj, 2)))

            written += 1

        await session.commit()

    return written


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(round(float(value), 4)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Module-level compatibility shims
# ---------------------------------------------------------------------------

_agent_instance: InjuryRiskAgent | None = None


def _get_agent(dry_run: bool = False) -> InjuryRiskAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = InjuryRiskAgent(dry_run=dry_run)
    return _agent_instance


async def run_for_team(team_abbr: str, dry_run: bool = False) -> int:
    return await _get_agent(dry_run).run_for_team(team_abbr)


async def run_all_teams(
    concurrency: int = 4, dry_run: bool = False, warehouse=None
) -> dict[str, int]:
    return await _get_agent(dry_run).run_all_teams(warehouse=warehouse, concurrency=concurrency)
