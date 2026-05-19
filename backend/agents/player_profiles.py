"""
Agent 3: Player Profiles — Synthesis Agent

Synthesizes ALL upstream agent outputs into forward-looking PPR projections.
Runs LAST in the pipeline (after team_systems, roster_changes, injury_risk,
schedule, and beat_reporter) so it has access to every signal.

Architecture:
  - Two-pass model: Haiku batch for stable veterans, Sonnet per-player for complex cases
  - Complex = rookies, dependency flags, contract year, high injury risk, beat reporter signals
  - Pattern: pre-aggregate in Python → Haiku batch OR Sonnet per-player → parse JSON → write DB
  - Never uses run_agent() (that is for live draft only)

Key outputs per player:
  - AI-driven projected_ppr_points (forward-looking, not historical average)
  - Role classification (wr1_alpha, workhorse, etc.)
  - Breakout candidate detection
  - Efficiency signal, age curve, situation score
  - Upside/downside PPR range (Sonnet players only)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import ClassVar
import pandas as pd
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU, SONNET
from backend.agents.team_systems import NFL_TEAMS
from backend.database import AsyncSessionLocal
from backend.integrations.nfl_data import normalize_player_name, build_player_lookup
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile, PlayerSchedule
from backend.models.dependency import PlayerDependency, BeatReporterSignal
from backend.utils.seasons import (
    get_current_season,
    get_analysis_seasons,
    get_analysis_year,
    get_player_seasons_for_baseline,
)

logger = logging.getLogger(__name__)

SKILL_POSITIONS = {"QB", "WR", "RB", "TE"}

PROFILE_STALENESS_DAYS = 30

# Increment whenever system prompts change to force regeneration of all profiles.
PLAYER_PROFILES_PROMPT_VERSION = "v4"


# ---------------------------------------------------------------------------
# Profile cache invalidation
# ---------------------------------------------------------------------------

def profile_needs_refresh(
    profile_updated_at: datetime | None,
    dep_updated_at: datetime | None = None,
    injury_updated_at: datetime | None = None,
    beat_signal_timestamps: list[datetime] | None = None,
    team_updated_at: datetime | None = None,
    team_system_updated_at: datetime | None = None,
    stored_prompt_version: str | None = None,
) -> bool:
    """Check if a player's profile needs regeneration.

    Returns True when:
      - No profile exists
      - Profile is older than PROFILE_STALENESS_DAYS
      - Dependency flags updated since last profile
      - Injury profile updated since last profile
      - Team changed since last profile (team_updated_at > profile.updated_at)
      - Team system re-graded since last profile (OLine, QB, scheme changes)
      - New high-confidence beat signals since last profile
      - Prompt version changed (system prompt was updated)
    """
    if profile_updated_at is None:
        return True

    if stored_prompt_version != PLAYER_PROFILES_PROMPT_VERSION:
        return True

    now = datetime.now(timezone.utc)
    if (now - profile_updated_at).days >= PROFILE_STALENESS_DAYS:
        return True

    if dep_updated_at and dep_updated_at > profile_updated_at:
        return True

    if injury_updated_at and injury_updated_at > profile_updated_at:
        return True

    if team_updated_at and team_updated_at > profile_updated_at:
        return True

    if team_system_updated_at and team_system_updated_at > profile_updated_at:
        return True

    for ts in (beat_signal_timestamps or []):
        if ts > profile_updated_at:
            return True

    return False


# ---------------------------------------------------------------------------
# Model routing — which players need Sonnet-level reasoning
# ---------------------------------------------------------------------------

def needs_sonnet_reasoning(player: dict) -> bool:
    """Returns True if this player needs full Sonnet reasoning rather than
    Haiku extraction.

    Haiku is appropriate for: stable veterans in the same system with no
    complexity signals.

    Sonnet is required for: anyone where history is an unreliable predictor
    of future value.
    """
    position = player.get("position", "")
    age = player.get("age") or 25

    # --- Explicit position rules ---

    # QBs always get Sonnet — they anchor entire offenses
    if position == "QB":
        return True

    # Rookies always get Sonnet (no NFL history)
    if player.get("is_rookie"):
        return True

    # --- Age-based decline thresholds ---
    # These positions age out fast — history overstates value for older players
    AGE_THRESHOLDS = {"RB": 28, "WR": 31, "TE": 31}
    age_threshold = AGE_THRESHOLDS.get(position, 32)
    if age >= age_threshold:
        return True

    # --- Situation changes ---

    # Dependency flags = role uncertainty
    if player.get("dependency_flags"):
        return True

    # Team change = new system, new QB, new role
    if player.get("team_changed_this_offseason"):
        return True

    # Contract year = motivation factor
    if player.get("contract_year"):
        return True

    # --- Risk signals ---

    injury = player.get("injury_profile", {})
    if injury.get("overall_risk_level") in ("high", "volatile"):
        return True
    if injury.get("pattern_flags"):
        return True

    # --- Career trajectory ---

    trajectory = player.get("career_trajectory", "")
    if trajectory in ("ascending", "declining", "breakout", "volatile"):
        return True

    # --- Market signals ---
    # League thinks this player is worth almost nothing — don't trust
    # historical average, force Sonnet to reason about the discrepancy
    league_price = player.get("market_value_league")
    if league_price is not None and league_price <= 5:
        return True

    # --- Beat reporter signals ---

    signals = player.get("beat_signals", [])
    if any(s.get("confidence") == "high" for s in signals):
        return True

    # Team compound risk
    team_sys = player.get("_team_system", {})
    if team_sys.get("compound_risk_flag"):
        return True

    # --- Elite producers ---
    # High-PPR players are high-stakes draft decisions; even "stable" ones
    # benefit from Sonnet reasoning about ceiling/floor and game-script nuance.
    _ELITE_PPR_PER_GAME = {"RB": 14.0, "WR": 14.0, "TE": 10.0}
    seasons = player.get("seasons", [])
    if seasons:
        best_ppg = max(
            (float(s.get("ppr_per_game") or 0) for s in seasons),
            default=0,
        )
        if best_ppg >= _ELITE_PPR_PER_GAME.get(position, 14.0):
            return True

    # Default: Haiku is sufficient — stable, same team, not aging out
    return False


# ---------------------------------------------------------------------------
# System prompts — Haiku (team batch) and Sonnet (per-player)
# ---------------------------------------------------------------------------

HAIKU_SYSTEM_PROMPT = """You are a fantasy football player analyst building a pre-draft research database.

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
  QB players → use only: qb_elite, qb_starter, qb_streamer, qb_backup
  WR players → use only: wr1_alpha, slot_specialist, deep_threat, possession_wr2, gadget
  TE players → use only: te1_inline, te1_pass_catcher, te2_blocker, te2_flex

  RB role_classification — use EXACTLY one. Apply the FIRST definition that fits:
    "workhorse" — 65%+ of team rushing attempts, OR 20+ carries/game avg, OR clear lead back
      with backup used only to spell. Most NFL teams have a lead back — this is the most common role.
      Examples: Derrick Henry, Saquon Barkley, Jonathan Taylor, Bijan Robinson.
    "featured_back" — 50-65% of carries AND catches passes (target share 8%+). Three-down role
      but not dominant enough for workhorse. Gets the bulk of work but shares with a change-of-pace back.
    "early_down_thumper" — 50-65% of carries but leaves field on 3rd down. Heavy run-game role,
      minimal receiving (target share under 8%).
    "pass_catching_specialist" — High snap share but under 40% of carries. Primary role is
      receiving out of backfield. Target share 12%+. Examples: Alvin Kamara, Austin Ekeler.
    "committee_back" — ONLY when no single back has 50%+ of carries. True split: two backs
      with 35-50% each, neither is clearly "the guy". Examples: true 50/50 timeshares.
      DO NOT use just because a backup exists — every team has a backup.
    "backup" — Under 35% of carries, no special receiving role. Handcuff or depth piece.
      Only gets meaningful work if the starter is injured.
  CRITICAL: If uncertain between workhorse and committee_back, choose workhorse.
  True committees are uncommon. Does one back get 55%+ of carries? Yes → workhorse, featured_back, or early_down_thumper.

Rules:
- anomalous_seasons_excluded: include year integers where data shows games < 10 OR backup_qb_season=true
- clean_season_baseline: project stats from non-excluded seasons. If no clean seasons exist, use all available.
  "ppr_points" is the projected season TOTAL (not per-game). Example: a WR2 over 17 games ≈ 200.0.
- breakout_flag = true if ANY of: Year 2 or 3 player, path opened by departure in dependency_flags,
  new scheme elevates this player type, efficiency metrics already exceed production statistics.
- situation_score: strong for high system grade + elite QB + no displacement;
  volatile or weak for displaced/committee flags or rookie QB; moderate otherwise.
- Age curve peaks: QB 26-32, RB 24-26, WR 24-29, TE 26-29. ascending = before peak; descending = past peak.
- Contract year flag (contract_year=true) → slight upward bias in trajectory.
- compound_risk_flag on team → all players lean toward volatile/weak situation_score.

Additional NGS efficiency data in player seasons (when present):
  avg_separation: average yards of separation at catch point; higher = elite route running.
  avg_yac_above_expectation: yards after catch above model expectation; positive = elite YAC.
  rush_yards_over_expected_per_att: rushing yards over expected; positive = above-avg vision/burst.
Use these numeric signals to inform separation_score, yards_after_catch_score, and efficiency_signal.

Additional Sleeper efficiency data in player seasons (when present):
  RB: rush_ypa (yards per carry, league avg ~4.2, elite 5.0+),
      rush_btkl (broken tackles, elite 20+/season),
      rush_fd (rushing first downs).
  WR/TE: rec_ypr (yards per reception, deep threat 14+, possession 10-),
         catch_pct (catch percentage, elite 70%+, concerning under 55%),
         rec_fd (receiving first downs).
  QB: cpoe (completion % above expectation, elite +3.0, poor -3.0),
      avg_time_to_throw (quick <2.7s, slow >3.2s).
  All: snap_count (offensive snaps for season).
Use these to assess whether production is efficiency-driven or volume-driven.

For players where all seasons show games=0 (rookies or new acquisitions with no NFL history):
  - Still include them; classify by position, team system, and dependency_flags.
  - Set career_trajectory="volatile" unless a clear signal exists.
  - Set clean_season_baseline to position-typical conservative estimates.
  - Set breakout_flag=true if any dependency_flag has type "beneficiary".

Output ONLY a valid JSON array. No explanation, no preamble, no markdown fences.
Your entire response must be parseable by json.loads()."""


ROOKIE_SONNET_SYSTEM = (
    "You are projecting a FIRST-YEAR NFL player's fantasy football season. "
    "Output ONLY valid JSON. No preamble. No markdown fences. "
    "Your entire response must be parseable by json.loads()."
)

ROOKIE_PROJECTION_PROMPT = """You are projecting a FIRST-YEAR NFL player's fantasy football season.

CRITICAL CONSTRAINTS — read before anything else:
- This player has ZERO NFL performance data
- Do NOT invent NFL statistics or reference NFL games this player has played
- Your ONLY statistical anchor is the college comp data provided below
- Confidence must be "low" or "medium" NEVER "high"
- All projections must be expressed as ranges (floor/ceiling), not point estimates
- Acknowledge uncertainty explicitly in reasoning

===============================================
PLAYER: {player_name}
Position: {position}
Team: {team} | Scheme: {scheme_type}
Age: {age} | Draft: Round {draft_round}, Pick {draft_pick}
===============================================

COLLEGE PRODUCTION:
{college_stats}

HISTORICAL COMPS (players with similar profiles who translated to the NFL):
{comp_data}
--- Translation rate for this draft capital + position:
    {translation_rate_summary}

LANDING SPOT ANALYSIS:
Team System Grade: {team_grade}
Scheme: {scheme_description}
QB Situation: {qb_name} ({qb_tier})

DEPTH CHART POSITION: {depth_chart_order}

DEPENDENCY FLAGS:
{dependency_flags}

INJURY RISK: {injury_risk_level}

SCHEDULE:
Early season (weeks 1-6): {early_schedule}
Fantasy playoffs (weeks 15-17): {playoff_schedule}

===============================================
TASK: Reason through this rookie's likely fantasy impact in year 1.

Structure your reasoning:
1. College comp translation — how well do the comps translate? What's the hit rate for this draft capital + position combo?
2. Landing spot — does the team situation accelerate or limit the comp projection?
3. Competition — is this player the clear starter or fighting for snaps?
4. Scheme fit — does the offensive system maximize or limit this player's skill set?
5. Projection — given all above, what is a realistic year-1 PPR range?

role_classification MUST match the player's position:
  QB → qb_elite, qb_starter, qb_streamer, qb_backup
  WR → wr1_alpha, slot_specialist, deep_threat, possession_wr2, gadget
  TE → te1_inline, te1_pass_catcher, te2_blocker, te2_flex
  RB → workhorse, featured_back, early_down_thumper, pass_catching_specialist, committee_back, backup

OUTPUT only valid JSON:
{{
  "projected_ppr_season": <float, midpoint>,
  "projected_ppr_floor": <float, pessimistic>,
  "projected_ppr_ceiling": <float, optimistic>,
  "projected_games": <int, 14-17>,
  "role_classification": <string>,
  "confidence": "low" | "medium",
  "breakout_probability": <float 0.0-1.0>,
  "comp_translation_grade": "A" | "B" | "C" | "D",
  "key_risks": [<string>, ...],
  "key_upside_factors": [<string>, ...],
  "projection_reasoning": <string, 3-5 sentences>
}}
"""

# Historical translation rates by position and round for the rookie prompt
_TRANSLATION_RATES: dict[tuple[str, int], str] = {
    ("RB", 1): "Top-10 RBs: 35% fantasy RB1 in year 1",
    ("RB", 2): "Rd2 RBs: 20% fantasy RB1 in year 1",
    ("WR", 1): "Top-10 WRs: 25% WR1 in year 1",
    ("WR", 2): "Rd2 WRs: 12% WR1 in year 1",
    ("TE", 1): "Top TE: 15% TE1 in year 1",
    ("QB", 1): "Top QB: 40% start year 1",
}


def _format_rookie_prompt(player: dict, team_context: dict) -> str:
    """Format the ROOKIE_PROJECTION_PROMPT with player-specific data."""
    position = player.get("position", "WR")
    draft_round = player.get("draft_round") or 2
    draft_pick = player.get("draft_pick") or 0
    comp_names = player.get("historical_comp_names", [])[:3]
    comp_yr1 = player.get("comp_yr1_avg_ppg") or 0
    comp_yr2 = player.get("comp_yr2_avg_ppg") or 0
    college_grade = player.get("college_profile_grade", "unknown")

    translation = _TRANSLATION_RATES.get(
        (position, min(draft_round, 2)),
        "Historical translation data limited for this draft slot"
    )

    # Format dependency flags
    dep_flags = player.get("dependency_flags", [])
    flags_text = "\n".join([
        f"- {f.get('type', f.get('flag_type', ''))}: "
        f"{f.get('reasoning', f.get('effect', ''))[:120]}"
        for f in dep_flags[:5]
    ]) or "No significant flags"

    injury = player.get("injury_profile", {})
    schedule = player.get("schedule", {})

    return ROOKIE_PROJECTION_PROMPT.format(
        player_name=player.get("name", "Unknown"),
        position=position,
        team=player.get("team_abbr", player.get("_team", "UNK")),
        scheme_type=team_context.get("oc_scheme", "unknown"),
        age=player.get("age", "unknown"),
        draft_round=draft_round,
        draft_pick=draft_pick,
        college_stats=f"Grade: {college_grade}, Capital: {player.get('draft_capital_signal', 'unknown')}",
        comp_data=(
            f"Comps: {', '.join(comp_names)}\n"
            f"Comp yr1 avg: {comp_yr1:.1f} PPG\n"
            f"Comp yr2 avg: {comp_yr2:.1f} PPG"
            if comp_names else "No historical comps available"
        ),
        translation_rate_summary=translation,
        team_grade=team_context.get("system_grade", "unknown"),
        scheme_description=(team_context.get("oc_scheme", "") or "")[:200],
        qb_name=team_context.get("qb_name", "unknown"),
        qb_tier=team_context.get("qb_tier", "unknown"),
        depth_chart_order=player.get("depth_chart_rank", "unknown"),
        dependency_flags=flags_text,
        injury_risk_level=injury.get("overall_risk_level", "unknown"),
        early_schedule=schedule.get("early_window_grade", "unknown"),
        playoff_schedule=schedule.get("playoff_window_grade", "unknown"),
    )


def _parse_rookie_sonnet_output(raw: str, player: dict) -> dict | None:
    """Parse rookie Sonnet JSON output with validation and fallback."""
    try:
        result = parse_json_output(raw)
        if isinstance(result, list) and result:
            result = result[0]
        if not isinstance(result, dict):
            return None

        # Validate required fields
        if "projected_ppr_season" not in result:
            logger.warning("Rookie Sonnet missing projected_ppr_season for %s", player.get("name"))
            return None

        # Enforce confidence constraint: never "high" for rookies
        if result.get("confidence") not in ("low", "medium"):
            result["confidence"] = "low"

        # Clamp breakout_probability
        bp = result.get("breakout_probability", 0.1)
        result["breakout_probability"] = max(0.0, min(1.0, float(bp)))

        # Validate comp_translation_grade
        if result.get("comp_translation_grade") not in ("A", "B", "C", "D"):
            result["comp_translation_grade"] = "C"

        return result
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning(
            "Rookie Sonnet invalid output for %s: %s",
            player.get("name"), str(exc)[:200],
        )
        return None


def _rookie_sonnet_fallback(player: dict) -> dict:
    """Fallback dict when rookie Sonnet call fails or returns invalid JSON."""
    comp_yr1 = player.get("comp_yr1_avg_ppg") or _ROOKIE_DEFAULT_PPG.get(player.get("position", "WR"), 8.0)
    midpoint = comp_yr1 * 14  # conservative 14-game assumption
    return {
        "projected_ppr_season": round(midpoint, 1),
        "projected_ppr_floor": round(midpoint * 0.6, 1),
        "projected_ppr_ceiling": round(midpoint * 1.4, 1),
        "projected_games": 14,
        "role_classification": "unknown",
        "confidence": "low",
        "breakout_probability": 0.10,
        "comp_translation_grade": "C",
        "key_risks": ["projection uncertain — Sonnet unavailable"],
        "key_upside_factors": [],
        "projection_reasoning": "Sonnet projection unavailable — using comp baseline.",
    }


SONNET_SYSTEM_PROMPT = """You are a fantasy football projection analyst. You synthesize ALL available context
to produce a forward-looking PPR projection for one NFL player.

You receive:
- Historical stats (3 seasons of per-game data)
- Team system context (OC scheme, QB tier, O-line grades, compound_risk_flag)
- Dependency flags (displaced, beneficiary, committee, etc. with impact reasoning)
- Injury risk profile (pattern flags, risk level, recovery assessment)
- Schedule grades (early/full/playoff windows)
- Beat reporter signals (practice reports, depth chart changes)

Your job: reason through ALL of these signals to produce a projected PPR total
for the upcoming 17-game season. This is a forward projection that accounts for
situation changes, role shifts, and risk factors — NOT a historical average.

Output ONLY a valid JSON object:
{
  "player_name": "string",
  "projected_ppr_points": float,
  "projection_reasoning": "2-3 sentences explaining the key factors driving this projection",
  "role_classification": "string",
  "separation_score": "string (elite/above_avg/avg/below_avg)",
  "yards_after_catch_score": "string (elite/above_avg/avg/below_avg)",
  "efficiency_signal": "string (elite/above_avg/avg/below_avg)",
  "age_curve_position": "string (ascending/peak/descending)",
  "career_trajectory": "string (breakout/rising/established/declining/volatile)",
  "anomalous_seasons_excluded": [int],
  "breakout_flag": boolean,
  "breakout_reasoning": "string or null",
  "positional_scarcity_tier": "string (scarce/moderate/deep)",
  "situation_score": "string (strong/moderate/weak/volatile)",
  "confidence": "string (high/medium/low)",
  "upside_ppr": float,
  "downside_ppr": float
}

role_classification MUST match the player's actual position — never cross-assign roles:
  QB players → use only: qb_elite, qb_starter, qb_streamer, qb_backup
  WR players → use only: wr1_alpha, slot_specialist, deep_threat, possession_wr2, gadget
  TE players → use only: te1_inline, te1_pass_catcher, te2_blocker, te2_flex

  RB role_classification — use EXACTLY one. Apply the FIRST definition that fits:
    "workhorse" — 65%+ of team rushing attempts, OR 20+ carries/game avg, OR clear lead back
      with backup used only to spell. Most NFL teams have a lead back — this is the most common role.
      Examples: Derrick Henry, Saquon Barkley, Jonathan Taylor, Bijan Robinson.
    "featured_back" — 50-65% of carries AND catches passes (target share 8%+). Three-down role
      but not dominant enough for workhorse. Gets the bulk of work but shares with a change-of-pace back.
    "early_down_thumper" — 50-65% of carries but leaves field on 3rd down. Heavy run-game role,
      minimal receiving (target share under 8%).
    "pass_catching_specialist" — High snap share but under 40% of carries. Primary role is
      receiving out of backfield. Target share 12%+. Examples: Alvin Kamara, Austin Ekeler.
    "committee_back" — ONLY when no single back has 50%+ of carries. True split: two backs
      with 35-50% each, neither is clearly "the guy". Examples: true 50/50 timeshares.
      DO NOT use just because a backup exists — every team has a backup.
    "backup" — Under 35% of carries, no special receiving role. Handcuff or depth piece.
      Only gets meaningful work if the starter is injured.
  CRITICAL: If uncertain between workhorse and committee_back, choose workhorse.
  True committees are uncommon. Does one back get 55%+ of carries? Yes → workhorse, featured_back, or early_down_thumper.

Rules:
- projected_ppr_points is a season TOTAL for 17 games.
  Typical ranges: QB elite ~350-400, WR1 alpha ~280-340, WR2 ~180-230, RB1 ~250-320, TE1 ~200-260.
- A beneficiary flag with departed_team trigger = MORE opportunity → project HIGHER than historical baseline.
- A displaced flag with active_and_healthy trigger = LESS opportunity → project LOWER.
- compound_risk_flag on team system = reduce all skill position projections by 10-15%.
- Injury risk "high" or "volatile" → weight downside more heavily, widen upside-downside gap.
- upside_ppr = realistic best-case 17-game total; downside_ppr = realistic worst-case.
- Age curve peaks: QB 26-32, RB 24-26, WR 24-29, TE 26-29.
- Contract year (contract_year=true) → slight upward trajectory bias.
- Do NOT invent specific injury events. If the injury data shows "no significant history",
  say exactly that. Only reference injuries explicitly listed in the provided injury risk profile.
  Never fabricate torn ACLs, hamstring tears, or other specific injuries not in the data.
- Output ONLY valid JSON. No preamble, no explanation, no markdown fences.
Your entire response must be parseable by json.loads().

Additional efficiency data in player seasons (when present):
  RB: rush_ypa (yards per carry, league avg ~4.2, elite 5.0+),
      rush_btkl (broken tackles, elite 20+/season),
      rush_fd (rushing first downs).
  WR/TE: rec_ypr (yards per reception, deep threat 14+, possession 10-),
         catch_pct (catch percentage, elite 70%+, concerning under 55%),
         rec_fd (receiving first downs).
  QB: cpoe (completion % above expectation, elite +3.0, poor -3.0),
      avg_time_to_throw (quick <2.7s, slow >3.2s).
  All: snap_count (offensive snaps for season).
Use these to assess whether production is efficiency-driven or volume-driven."""


# ---------------------------------------------------------------------------
# PlayerProfilesAgent
# ---------------------------------------------------------------------------

class PlayerProfilesAgent(BaseAgent):
    AGENT_NAME       = "player_profiles"
    AGENT_MODEL      = HAIKU
    AGENT_MAX_TOKENS = 4000

    # Limit concurrent Sonnet calls across all teams to avoid 529 bursts.
    # 4 teams × 3+ Sonnet players each = 12+ simultaneous requests → 529.
    _sonnet_semaphore: ClassVar[asyncio.Semaphore] = asyncio.Semaphore(6)

    # ------------------------------------------------------------------
    # Sonnet rate-limited wrapper
    # ------------------------------------------------------------------

    async def _call_sonnet_with_limit(
        self,
        system: str,
        user: str,
        entity_id: str,
        input_data: dict,
        max_tokens: int = 800,
    ) -> str:
        """Call Sonnet through the shared semaphore (max 2 concurrent)."""
        async with self._sonnet_semaphore:
            return await self.call_once(
                system=system,
                user=user,
                input_data=input_data,
                entity_id=entity_id,
                model=SONNET,
                max_tokens=max_tokens,
            )

    # ------------------------------------------------------------------
    # Sonnet health check
    # ------------------------------------------------------------------

    async def _check_sonnet_available(self) -> bool:
        """Quick Sonnet ping before the full run.

        If Sonnet is down, complex players fall back to Haiku instead of
        being skipped entirely. Better a Haiku profile than no profile.
        """
        try:
            await self._client.messages.create(
                model=SONNET,
                max_tokens=10,
                system="Respond with OK",
                messages=[{"role": "user", "content": "ping"}],
            )
            logger.info("Sonnet health check: OK")
            return True
        except Exception as e:
            logger.warning(
                "Sonnet health check failed: %s — complex players will use Haiku fallback",
                e,
            )
            return False

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
            entry["contract_year"] = bool(row.get("contract_year", False))
            # Include nfl gsis player_id for reliable cross-source matching
            pid = row.get("player_id")
            if pid and pd.notna(pid):
                entry["nfl_player_id"] = str(pid).strip()
            result.append(entry)

        return result

    def _is_backup_qb_season(self, team: str, season: int) -> bool:
        """Returns True only if the team had a meaningful backup QB situation —
        not garbage-time mop-up appearances.

        Threshold: backup must have >= 30 pass attempts OR starter must have
        played < 14 games.

        This prevents false positives like a starter playing all 17 games
        while a backup appears in a few garbage-time mop-up games.
        """
        try:
            qb_stats = self._warehouse.get_qb_stats(season)
            if qb_stats is None or qb_stats.empty:
                return False

            # Get all QBs for this team
            team_col = next(
                (c for c in ("recent_team", "team") if c in qb_stats.columns),
                None,
            )
            if not team_col:
                return False

            team_qbs = qb_stats[qb_stats[team_col] == team]
            if team_qbs.empty:
                return False

            # Find starter = QB with most attempts (fall back to passing_yards
            # for Sleeper seasons where attempts is NA)
            att_col = next(
                (c for c in ("attempts", "pass_att")
                 if c in team_qbs.columns and team_qbs[c].notna().any()),
                None,
            )
            sort_col = att_col or (
                "passing_yards" if "passing_yards" in team_qbs.columns else None
            )
            if not sort_col:
                return False

            team_qbs = team_qbs.sort_values(sort_col, ascending=False)
            if len(team_qbs) < 2:
                return False  # Only one QB

            starter = team_qbs.iloc[0]
            backup = team_qbs.iloc[1]

            # Check games for starter
            games_col = next(
                (c for c in ("games", "gp") if c in team_qbs.columns),
                None,
            )
            starter_games = (
                float(starter.get(games_col, 17) or 17) if games_col else 17
            )

            # Determine if backup had meaningful playing time
            if att_col:
                backup_att = float(backup.get(att_col, 0) or 0)
                meaningful_backup = backup_att >= 30
            else:
                # Sleeper fallback: use passing_yards as proxy
                # 30 attempts × ~6.5 ypa ≈ 195 yards
                backup_yds = float(backup.get("passing_yards", 0) or 0)
                meaningful_backup = backup_yds >= 200

            injured_starter = starter_games < 14

            return meaningful_backup or injured_starter

        except Exception as e:
            logger.warning(
                "_is_backup_qb_season failed for %s %d: %s",
                team, season, e,
            )
            return False

    def _get_player_season_stats(
        self, player_name: str, team: str, season: int,
        position: str,
        nfl_player_id: str | None = None,
        sleeper_id: str | None = None,
        sportradar_id: str | None = None,
    ) -> dict | None:
        """Return compact season stats for one player from the cached target_share df.

        Position is REQUIRED to prevent cross-position name collisions
        (e.g. "B.Taylor" WR on IND must NOT match "J.Taylor" RB on IND).

        Match priority:
          1. player_id column (gsis id) — 100% reliable, no name ambiguity
          2. last-name + team + SAME POSITION — handles most veterans reliably
          3. last-name + first-initial cross-team + SAME POSITION fallback —
             ONLY when exactly ONE unique player_id at this position
        """
        ts_df = self._warehouse.get_target_share(season)
        if ts_df is None:
            return None

        pos_upper = position.upper()
        has_position_col = "position" in ts_df.columns

        def _pos_filter(df: pd.DataFrame) -> pd.DataFrame:
            """Filter to same position. Critical to prevent cross-position collisions."""
            if has_position_col:
                return df[df["position"].str.upper() == pos_upper]
            return df

        def _extract(row: pd.Series) -> dict | None:
            games = int(row.get("games", 0) or 0)
            if games == 0:
                return None
            def _f(col: str, decimals: int = 3):
                v = row.get(col)
                try:
                    return round(float(v), decimals) if v is not None and pd.notna(v) else None
                except (TypeError, ValueError):
                    return None
            targets = int(row.get("total_targets", 0) or 0)
            receptions = int(row.get("total_receptions", 0) or 0)
            return {
                "games":           games,
                "recent_team":     str(row.get("recent_team", "") or ""),
                "target_share":    _f("avg_target_share"),
                "air_yards_share": _f("avg_air_yards_share"),
                "targets":         targets,
                "receptions":      receptions,
                "rec_yards":       int(row.get("total_rec_yards",  0) or 0),
                "rec_tds":         int(row.get("total_rec_tds",    0) or 0),
                "carries":         int(row.get("total_carries",    0) or 0),
                "rush_yards":      int(row.get("total_rush_yards", 0) or 0),
                "rush_tds":        int(row.get("total_rush_tds",   0) or 0),
                "ppr_per_game":    _f("ppr_per_game", 1),
                # Efficiency fields
                "rush_ypa":        _f("rush_ypa", 2),
                "rush_btkl":       _f("rush_btkl", 0),
                "rec_ypr":         _f("rec_ypr", 2),
                "snap_count":      _f("off_snp", 0),
                "rush_fd":         _f("rush_fd", 0),
                "rec_fd":          _f("rec_fd", 0),
                "catch_pct": (
                    round(receptions / targets * 100, 1)
                    if targets > 0 else None
                ),
            }

        def _extract_combined(rows: pd.DataFrame) -> dict | None:
            """Aggregate stats across multi-team splits for the same player."""
            def _int_sum(col: str) -> int:
                return int(rows[col].fillna(0).sum()) if col in rows.columns else 0

            total_games = _int_sum("games")
            if total_games == 0:
                return None

            # Use the team with the most games as the primary team
            primary_team = rows.loc[rows["games"].fillna(0).astype(int).idxmax(), "recent_team"]

            # Games-weighted average for rate stats
            game_weights = rows["games"].fillna(0).astype(float)
            weight_sum = game_weights.sum()

            def _weighted_avg(col: str, decimals: int = 3):
                if col not in rows.columns:
                    return None
                vals = rows[col].apply(
                    lambda v: float(v) if v is not None and pd.notna(v) else 0.0
                )
                avg = (vals * game_weights).sum() / weight_sum if weight_sum > 0 else 0.0
                return round(avg, decimals) if avg else None

            receptions = _int_sum("total_receptions")
            rec_yards = _int_sum("total_rec_yards")
            rec_tds = _int_sum("total_rec_tds")
            rush_yards = _int_sum("total_rush_yards")
            rush_tds = _int_sum("total_rush_tds")

            # PPR cross-validation against source fantasy_points_ppr
            computed_ppr = receptions * 1.0 + (rec_yards + rush_yards) * 0.1 + (rec_tds + rush_tds) * 6.0
            fantasy_ppr = float(rows["total_fantasy_points"].fillna(0).sum()) if "total_fantasy_points" in rows.columns else 0.0
            if fantasy_ppr > 0 and abs(computed_ppr - fantasy_ppr) / fantasy_ppr > 0.15:
                pid = rows.iloc[0].get("player_id", "?")
                logger.warning(
                    "PPR divergence for player_id=%s: computed=%.1f vs source=%.1f",
                    pid, computed_ppr, fantasy_ppr,
                )

            total_targets = _int_sum("total_targets")
            return {
                "games":           total_games,
                "recent_team":     str(primary_team or ""),
                "target_share":    _weighted_avg("avg_target_share"),
                "air_yards_share": _weighted_avg("avg_air_yards_share"),
                "targets":         total_targets,
                "receptions":      receptions,
                "rec_yards":       rec_yards,
                "rec_tds":         rec_tds,
                "carries":         _int_sum("total_carries"),
                "rush_yards":      rush_yards,
                "rush_tds":        rush_tds,
                "ppr_per_game":    _weighted_avg("ppr_per_game", 1),
                # Efficiency fields
                "rush_ypa":        _weighted_avg("rush_ypa", 2),
                "rush_btkl":       _int_sum("rush_btkl") or None,
                "rec_ypr":         _weighted_avg("rec_ypr", 2),
                "snap_count":      _int_sum("off_snp") or None,
                "rush_fd":         _int_sum("rush_fd") or None,
                "rec_fd":          _int_sum("rec_fd") or None,
                "catch_pct": (
                    round(receptions / total_targets * 100, 1)
                    if total_targets > 0 else None
                ),
            }

        # --- Path 0a: sleeper_id match (best — 100% coverage from Sleeper) ---
        if sleeper_id and "sleeper_id" in ts_df.columns:
            id_rows = ts_df[ts_df["sleeper_id"] == sleeper_id]
            if not id_rows.empty:
                if len(id_rows) == 1:
                    return _extract(id_rows.iloc[0])
                return _extract_combined(id_rows)

        # --- Path 0b: sportradar_id match (98% coverage) ---
        if sportradar_id and "sportradar_id" in ts_df.columns:
            id_rows = ts_df[ts_df["sportradar_id"] == sportradar_id]
            if not id_rows.empty:
                if len(id_rows) == 1:
                    return _extract(id_rows.iloc[0])
                return _extract_combined(id_rows)

        # --- Path 1: player_id match (gsis_id — 29% coverage) ---
        if nfl_player_id and "player_id" in ts_df.columns:
            id_rows = ts_df[ts_df["player_id"] == nfl_player_id]
            if not id_rows.empty:
                if len(id_rows) == 1:
                    return _extract(id_rows.iloc[0])
                # Multiple rows = multi-team season — aggregate across splits
                return _extract_combined(id_rows)

        # --- Path 2: last-name + team + SAME POSITION ---
        last = player_name.split()[-1]
        mask = (
            ts_df["player_name"].str.contains(last, case=False, na=False) &
            (ts_df["recent_team"] == team)
        )
        rows = _pos_filter(ts_df[mask]).sort_values("games", ascending=False)

        # Always disambiguate by first initial — even with one match,
        # a different initial means a different player (e.g. Isaiah Jacobs
        # vs Josh Jacobs on GB).  Handles both abbreviated ("D.Samuel")
        # and full name ("Deebo Samuel") formats.
        if not rows.empty:
            first_initial = player_name.split()[0][0].upper()
            initial_rows = rows[
                rows["player_name"].str[0].str.upper() == first_initial
            ]
            if not initial_rows.empty:
                rows = initial_rows
            elif nfl_player_id and "player_id" in rows.columns:
                # Initial mismatch — verify by ID before attributing
                id_rows = rows[rows["player_id"] == nfl_player_id]
                if not id_rows.empty:
                    rows = id_rows
                else:
                    rows = rows.iloc[0:0]  # ID mismatch — wrong player
            elif len(rows) == 1:
                # Single match, wrong initial, no ID to verify — refuse
                rows = rows.iloc[0:0]

        if not rows.empty:
            return _extract(rows.iloc[0])

        # --- Path 3: cross-team fallback (pre-trade history) + SAME POSITION ---
        # Only use when the caller has a known nfl_player_id that matches
        # the candidate's player_id. Without an ID to verify, cross-team
        # fallback risks attributing stats from a different player who
        # shares the same initial+last name (e.g. J'Mari Taylor ≠ Jonathan Taylor).
        if not nfl_player_id:
            return None  # No ID to verify — refuse cross-team attribution

        first_initial = player_name.split()[0][0].upper()
        all_last = _pos_filter(
            ts_df[ts_df["player_name"].str.contains(last, case=False, na=False)]
        )
        initial_fallback = all_last[all_last["player_name"].str.startswith(f"{first_initial}.")]
        candidates = initial_fallback if not initial_fallback.empty else all_last

        if "player_id" in candidates.columns:
            # Verify the candidate's player_id matches the caller's ID
            id_match = candidates[candidates["player_id"] == nfl_player_id]
            if not id_match.empty:
                return _extract(id_match.sort_values("games", ascending=False).iloc[0])
            # ID mismatch — different player with same name
            return None
        elif len(candidates["player_name"].unique()) != 1:
            return None  # No player_id column but multiple name variants

        if candidates.empty:
            return None
        return _extract(candidates.sort_values("games", ascending=False).iloc[0])

    def _get_qb_season(
        self, player_name: str, team: str, season: int,
        nfl_player_id: str | None = None,
    ) -> dict | None:
        """Return QB-specific season stats from cached qb_season data."""
        qb_df = self._warehouse.get_qb_stats(season)
        if qb_df is None:
            return None

        # Match by player_id first (most reliable)
        match = pd.DataFrame()
        if nfl_player_id and "player_id" in qb_df.columns:
            match = qb_df[qb_df["player_id"] == nfl_player_id]

        # Fallback: name + team
        if match.empty:
            normalized = normalize_player_name(player_name)
            if "player_name" in qb_df.columns:
                qb_df_team = qb_df[qb_df["recent_team"] == team]
                for _, row in qb_df_team.iterrows():
                    if normalize_player_name(str(row.get("player_name", ""))) == normalized:
                        match = qb_df_team[qb_df_team.index == row.name]
                        break

        if match.empty:
            return None

        row = match.iloc[0]
        _g = row.get("games", 0)
        games = 0 if _g is None or (hasattr(_g, '__class__') and pd.isna(_g)) else int(_g)
        if games == 0:
            return None

        def _safe_float(col: str, decimals: int = 1):
            v = row.get(col)
            try:
                return round(float(v), decimals) if v is not None and pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        def _safe_int(col: str, default: int = 0) -> int:
            v = row.get(col, default)
            if v is None or (hasattr(v, '__class__') and pd.isna(v)):
                return default
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        return {
            "games":              games,
            "recent_team":        str(row.get("recent_team", team)),
            "completions":        _safe_int("completions"),
            "attempts":           _safe_int("attempts"),
            "completion_pct":     _safe_float("completion_pct", 3),
            "passing_yards":      _safe_int("passing_yards"),
            "passing_tds":        _safe_int("passing_tds"),
            "interceptions":      _safe_int("interceptions"),
            "sacks":              _safe_int("sacks"),
            "cpoe":               _safe_float("cpoe", 2),
            "avg_time_to_throw":  _safe_float("avg_time_to_throw", 3),
            "rushing_yards":      _safe_int("rushing_yards"),
            "rushing_tds":        _safe_int("rushing_tds"),
            "carries":            _safe_int("carries"),
            "fantasy_points_ppr": _safe_float("fantasy_points_ppr", 1),
            "ppr_per_game":       _safe_float("ppr_per_game", 1),
        }

    def _get_snap_pct(self, player_name: str, team: str, season: int) -> float | None:
        """Return avg offensive snap % from the cached snap_pct df."""
        snap_df = self._warehouse.get_snap_pct(season)
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
        ngs = self._warehouse.get_ngs_receiving(season)
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
        ngs = self._warehouse.get_ngs_rushing(season)
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

    def _get_ngs_passing_stats(self, player_name: str, team: str, season: int) -> dict:
        """Return NGS passing metrics (CPOE, time to throw, aggressiveness)."""
        ngs = self._warehouse.get_ngs_passing(season)
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
        for col in ("completion_percentage_above_expectation", "avg_time_to_throw", "aggressiveness"):
            v = row.get(col)
            try:
                if v is not None and pd.notna(v):
                    result[col] = round(float(v), 2)
            except (TypeError, ValueError):
                pass
        # Rename CPOE to shorter key for prompt
        if "completion_percentage_above_expectation" in result:
            result["cpoe"] = result.pop("completion_percentage_above_expectation")
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

    async def _get_db_team_players(self, team: str) -> list[dict]:
        """
        Get all skill-position players assigned to this team in the DB.
        Used to supplement the nfl_data_py roster with offseason moves.
        """
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Player).where(
                        Player.team_abbr == team,
                        Player.position.in_(SKILL_POSITIONS),
                    )
                )
                players = result.scalars().all()
        except Exception as exc:
            logger.debug("Could not fetch DB team players for %s: %s", team, exc)
            return []

        result = []
        for p in players:
            entry = {"name": p.name, "position": p.position, "age": p.age}
            if p.gsis_id:
                entry["nfl_player_id"] = p.gsis_id
            elif p.yahoo_player_id and p.yahoo_player_id.startswith("nfl_"):
                entry["nfl_player_id"] = p.yahoo_player_id[4:]
            if p.sleeper_id:
                entry["sleeper_id"] = p.sleeper_id
            if p.sportradar_id:
                entry["sportradar_id"] = p.sportradar_id
            if p.nfl_seasons_played is not None:
                entry["nfl_seasons_played"] = p.nfl_seasons_played
            result.append(entry)
        return result

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
                "position":             p.position,
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

    async def _get_team_injury_profiles(self, team: str) -> dict[str, dict]:
        """Return {player_name: injury_profile_dict} for all players on team.

        One DB query per team — no N+1.
        """
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(PlayerInjuryProfile, Player.name)
                    .join(Player, PlayerInjuryProfile.player_id == Player.id)
                    .where(Player.team_abbr == team)
                )
                rows = result.all()
        except Exception as exc:
            logger.debug("Could not load injury profiles for %s: %s", team, exc)
            return {}

        profiles: dict[str, dict] = {}
        for ip, name in rows:
            profiles[name] = {
                "overall_risk_level": ip.overall_risk_level,
                "risk_adjusted_value_modifier": float(ip.risk_adjusted_value_modifier or 0),
                "pattern_flags": ip.pattern_flags or [],
                "chronic_conditions": ip.chronic_conditions or [],
                "workload_cliff_flag": ip.workload_cliff_flag,
                "high_mileage_flag": ip.high_mileage_flag,
                "post_acl_flag": ip.post_acl_flag,
                "concussion_count": ip.concussion_count,
                "recovery_assessment": ip.recovery_assessment,
                "risk_notes": ip.risk_notes,
            }
        return profiles

    async def _get_team_schedules(self, team: str) -> dict[str, dict]:
        """Return {player_name: schedule_dict} for all players on team.

        One DB query per team — no N+1.
        """
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(PlayerSchedule, Player.name)
                    .join(Player, PlayerSchedule.player_id == Player.id)
                    .where(Player.team_abbr == team)
                )
                rows = result.all()
        except Exception as exc:
            logger.debug("Could not load schedules for %s: %s", team, exc)
            return {}

        schedules: dict[str, dict] = {}
        for sched, name in rows:
            schedules[name] = {
                "early_window_grade": sched.early_window_grade,
                "full_season_grade": sched.full_season_grade,
                "playoff_window_grade": sched.playoff_window_grade,
                "schedule_score": float(sched.schedule_score or 0),
                "bye_week": sched.bye_week,
                "bye_in_playoff_window": sched.bye_in_playoff_window,
            }
        return schedules

    async def _get_team_beat_signals(self, team: str) -> dict[str, list[dict]]:
        """Return {player_name: [signal_dicts]} for all players on team.

        One DB query per team — no N+1.
        """
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BeatReporterSignal, Player.name)
                    .join(Player, BeatReporterSignal.player_id == Player.id)
                    .where(Player.team_abbr == team)
                )
                rows = result.all()
        except Exception as exc:
            logger.debug("Could not load beat signals for %s: %s", team, exc)
            return {}

        signals: dict[str, list[dict]] = {}
        for sig, name in rows:
            signals.setdefault(name, []).append({
                "signal_type": sig.signal_type,
                "raw_text": sig.raw_text,
                "confidence": sig.confidence,
                "source": sig.source,
            })
        return signals

    async def _get_team_market_values(self, team: str) -> dict[str, float]:
        """Return {player_name: best_market_value} for players on team.

        Includes players with either league or FantasyPros market value,
        so the skip-logic keeps all fantasy-relevant players.
        """
        try:
            async with AsyncSessionLocal() as session:
                from sqlalchemy import or_

                result = await session.execute(
                    select(
                        Player.name,
                        Player.market_value_league,
                        Player.market_value_fantasypros,
                    )
                    .where(Player.team_abbr == team)
                    .where(
                        or_(
                            Player.market_value_league.isnot(None),
                            Player.market_value_fantasypros.isnot(None),
                        )
                    )
                )
                return {
                    name: float(league or fp or 0)
                    for name, league, fp in result.all()
                }
        except Exception as exc:
            logger.debug("Could not load market values for %s: %s", team, exc)
            return {}

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
    # Context builder — all Python, zero API calls
    # ------------------------------------------------------------------

    async def _build_team_context(self, team_abbr: str) -> dict:
        team             = team_abbr.upper()
        analysis_seasons = get_analysis_seasons(3)
        current_season   = get_current_season()
        analysis_year    = get_analysis_year()

        team_system      = await self._get_team_system(team)
        dep_flags        = await self._get_team_dependency_flags(team)
        rookie_fields    = await self._get_team_rookie_fields(team)
        injury_profiles  = await self._get_team_injury_profiles(team)
        schedules        = await self._get_team_schedules(team)
        beat_signals     = await self._get_team_beat_signals(team)
        market_values    = await self._get_team_market_values(team)

        backup_qb_flags = {
            s: self._is_backup_qb_season(team, s) for s in analysis_seasons
        }

        roster = self._get_team_roster(team, current_season)

        # Inject DB players not in nfl_data_py rosters.
        # This catches: rookies (not yet in roster data), offseason trades/signings
        # (nfl_data_py reflects prior season teams, DB has current assignments),
        # and players with zero prior-season data like redshirt rookies.
        db_team_players = await self._get_db_team_players(team)
        roster_names = {r["name"] for r in roster}
        # Build nfl_seasons_played lookup from DB for per-player lookback
        nfl_seasons_lookup: dict[str, int | None] = {
            dbp["name"]: dbp.get("nfl_seasons_played")
            for dbp in db_team_players
        }
        # Enrich existing roster entries with IDs from DB players.
        # nfl_data_py roster lacks sleeper_id/sportradar_id — without
        # these, _get_player_season_stats() falls back to fragile
        # last-name matching which can pick the wrong player
        # (e.g. Brian Robinson instead of Bijan Robinson on ATL).
        db_by_name = {dbp["name"]: dbp for dbp in db_team_players}
        for r in roster:
            if dbp := db_by_name.get(r["name"]):
                r.setdefault("sleeper_id", dbp.get("sleeper_id"))
                r.setdefault("sportradar_id", dbp.get("sportradar_id"))
                if not r.get("nfl_player_id"):
                    r["nfl_player_id"] = dbp.get("nfl_player_id")
        for dbp in db_team_players:
            if dbp["name"] not in roster_names:
                roster.append(dbp)

        seen:    set[str]   = set()
        players: list[dict] = []
        depth_players: list[dict] = []  # fringe players with no stats → depth profile only

        for info in roster:
            pname = info["name"]
            if pname in seen:
                continue
            seen.add(pname)

            nfl_pid = info.get("nfl_player_id")
            pos = info["position"]

            # Look up depth chart rank for this player
            depth_rank = None
            if nfl_pid and self._warehouse:
                depth_rank = self._warehouse.get_player_depth_rank(nfl_pid)

            seasons_data: list[dict] = []

            # Dynamic per-player lookback: load enough seasons to get
            # 4 clean ones after injury exclusion, capped by career length
            player_seasons = get_player_seasons_for_baseline(
                nfl_seasons_lookup.get(pname)
            )

            if pos == "QB":
                # QB branch: use QB-specific passing stats + NGS passing
                for season in player_seasons:
                    stats = self._get_qb_season(pname, team, season, nfl_player_id=nfl_pid)
                    if stats:
                        ngs = self._get_ngs_passing_stats(pname, team, season)
                        if ngs:
                            stats.update(ngs)
                        stats["year"] = season
                        stats = {k: v for k, v in stats.items() if v is not None}
                        seasons_data.append(stats)
                    else:
                        seasons_data.append({"year": season, "games": 0, "note": "no data"})
            else:
                # WR/RB/TE branch: use target_share data
                for season in player_seasons:
                    stats = self._get_player_season_stats(
                        pname, team, season, position=pos,
                        nfl_player_id=nfl_pid,
                        sleeper_id=info.get("sleeper_id"),
                        sportradar_id=info.get("sportradar_id"),
                    )
                    if stats:
                        stats["year"]             = season
                        # Only apply backup_qb flag to WRs/TEs whose production is
                        # depressed by backup QB play.  RBs are largely unaffected.
                        stat_team = stats.get("recent_team", team)
                        stats["backup_qb_season"] = (
                            backup_qb_flags.get(season, False)
                            if pos in ("WR", "TE") and stat_team.upper() == team.upper()
                            else False
                        )
                        # Attach NGS efficiency data per position
                        if pos in ("WR", "TE"):
                            ngs = self._get_ngs_receiving_stats(pname, team, season)
                            if ngs:
                                stats.update(ngs)
                        elif pos == "RB":
                            ngs = self._get_ngs_rushing_stats(pname, team, season)
                            if ngs:
                                stats.update(ngs)
                        stats = {k: v for k, v in stats.items() if v is not None}
                        seasons_data.append(stats)
                    else:
                        seasons_data.append({
                            "year":             season,
                            "games":            0,
                            "backup_qb_season": backup_qb_flags.get(season, False),
                            "note":             "no data",
                        })

            # Skip only players with zero history AND no dependency flags AND not a rookie
            # AND no market value.  Rookies get profiled from college data; players with
            # market value are clearly fantasy-relevant even without game history (e.g.
            # redshirt rookies, players returning from injury).  Pure depth with nothing
            # to say are skipped.
            has_any_data     = any(s.get("games", 0) > 0 for s in seasons_data)
            has_flags        = bool(dep_flags.get(pname, []))
            is_rookie_player = pname in rookie_fields
            has_market_value = pname in market_values
            if not has_any_data and not has_flags and not is_rookie_player and not has_market_value:
                continue

            # Guard: non-rookie with zero game data in ALL seasons should NOT be
            # sent to the AI model — it will hallucinate stats.  Instead, mark
            # for depth-only profiling (written to DB in a separate pass).
            # Exception: players with market_value are fantasy-relevant (e.g.
            # returning from injury/suspension) and should go to the AI.
            if not has_any_data and not is_rookie_player and not has_market_value:
                depth_players.append({
                    "name":           pname,
                    "position":       pos,
                    "nfl_player_id":  nfl_pid,
                    "depth_chart_rank": depth_rank,
                })
                continue

            # Detect team change: compare most recent season's team to current
            team_changed = False
            for s in sorted(seasons_data, key=lambda x: x.get("year", 0), reverse=True):
                prev_team = s.get("recent_team", "")
                if s.get("games", 0) > 0 and prev_team:
                    team_changed = prev_team.upper() != team.upper()
                    break

            player_entry: dict = {
                "name":             pname,
                "position":         info["position"],
                "age":              info.get("age"),
                "contract_year":    info.get("contract_year", False),
                "snap_pct":         self._get_snap_pct(pname, team, current_season),
                "seasons":          seasons_data,
                "dependency_flags": dep_flags.get(pname, []),
                "nfl_player_id":    nfl_pid,  # pass through for DB ID resolution
                "depth_chart_rank": depth_rank,  # 1=starter, 2=backup, None=unknown
                # Upstream agent context (for needs_sonnet_reasoning + AI projection)
                "injury_profile":   injury_profiles.get(pname, {}),
                "schedule":         schedules.get(pname, {}),
                "beat_signals":     beat_signals.get(pname, []),
                "_team_system":     team_system,
                # Sonnet routing signals
                "team_changed_this_offseason": team_changed,
                "market_value_league": market_values.get(pname),
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
            "depth_players": depth_players,
        }

    # ------------------------------------------------------------------
    # Cache invalidation — skip players whose profiles are current
    # ------------------------------------------------------------------

    async def _get_stale_players(self, team: str, force: bool = False) -> set[str] | None:
        """Return names of players whose profiles need regeneration.

        Returns None when force=True (meaning regenerate everyone).
        """
        if force:
            return None

        async with AsyncSessionLocal() as session:
            players = (
                await session.execute(select(Player).where(Player.team_abbr == team))
            ).scalars().all()

            if not players:
                return set()

            player_ids = [p.id for p in players]

            # Existing profiles keyed by player_id
            profiles: dict = {}
            result = (
                await session.execute(
                    select(PlayerProfile).where(PlayerProfile.player_id.in_(player_ids))
                )
            ).scalars().all()
            for pr in result:
                profiles[pr.player_id] = pr

            # Latest dependency updated_at per player
            dep_latest: dict = {}
            deps = (
                await session.execute(
                    select(PlayerDependency).where(PlayerDependency.player_id.in_(player_ids))
                )
            ).scalars().all()
            for d in deps:
                existing = dep_latest.get(d.player_id)
                if existing is None or d.updated_at > existing:
                    dep_latest[d.player_id] = d.updated_at

            # Injury profiles
            injuries: dict = {}
            inj_result = (
                await session.execute(
                    select(PlayerInjuryProfile).where(
                        PlayerInjuryProfile.player_id.in_(player_ids)
                    )
                )
            ).scalars().all()
            for i in inj_result:
                injuries[i.player_id] = i

            # High-confidence beat signals
            beat_times: dict[object, list[datetime]] = {}
            bs_result = (
                await session.execute(
                    select(BeatReporterSignal).where(
                        BeatReporterSignal.player_id.in_(player_ids),
                        BeatReporterSignal.confidence == "high",
                    )
                )
            ).scalars().all()
            for s in bs_result:
                beat_times.setdefault(s.player_id, []).append(s.flagged_at)

            # Team system updated_at — keyed by team_abbr
            from backend.models.team_system import TeamSystem
            from backend.utils.seasons import get_current_season
            ts_updated: dict[str, datetime] = {}
            team_abbrs = {p.team_abbr for p in players if p.team_abbr}
            if team_abbrs:
                ts_result = (
                    await session.execute(
                        select(TeamSystem).where(
                            TeamSystem.team_abbr.in_(team_abbrs),
                            TeamSystem.season_year == get_current_season(),
                        )
                    )
                ).scalars().all()
                for ts in ts_result:
                    ts_updated[ts.team_abbr] = ts.updated_at

            stale: set[str] = set()
            for p in players:
                prof = profiles.get(p.id)
                stored_ver = None
                if prof and prof.clean_season_baseline:
                    stored_ver = prof.clean_season_baseline.get("prompt_version")
                if profile_needs_refresh(
                    profile_updated_at=prof.updated_at if prof else None,
                    dep_updated_at=dep_latest.get(p.id),
                    injury_updated_at=injuries[p.id].updated_at if p.id in injuries else None,
                    beat_signal_timestamps=beat_times.get(p.id, []),
                    team_updated_at=getattr(p, "team_updated_at", None),
                    team_system_updated_at=ts_updated.get(p.team_abbr),
                    stored_prompt_version=stored_ver,
                ):
                    stale.add(p.name)

            return stale

    # ------------------------------------------------------------------
    # Per-team runner — two-pass: Haiku batch + Sonnet per-player
    # ------------------------------------------------------------------

    async def run_for_team(self, team_abbr: str, force: bool = False) -> int:
        """Run for one team. Returns number of profile records written.

        Two-pass architecture:
          1. Haiku batch — stable veterans with no complex signals
          2. Sonnet per-player — rookies, flagged, contract year, high injury, etc.

        When force=False, only regenerates profiles where upstream data has changed.
        """
        if self._warehouse is None:
            from backend.integrations.nfl_data import NflDataWarehouse
            self._warehouse = NflDataWarehouse.build()
        team = team_abbr.upper()
        logger.info("Building player profiles context for %s", team)

        try:
            stale_names = await self._get_stale_players(team, force)

            context = await self._build_team_context(team)

            if not context["players"]:
                logger.info("%s: no skill-position players with data, skipping", team)
                return 0

            # Filter to only stale players (unless force mode)
            if stale_names is not None:
                original_count = len(context["players"])
                context["players"] = [
                    p for p in context["players"] if p["name"] in stale_names
                ]
                skipped = original_count - len(context["players"])
                if skipped > 0:
                    logger.info(
                        "%s: %d cached, %d need refresh", team, skipped, len(context["players"])
                    )
                if not context["players"]:
                    logger.info("%s: all profiles current, skipping API calls", team)
                    return 0

            # Split players: Haiku batch vs Sonnet individual
            # When Sonnet is unavailable, all players go through Haiku.
            sonnet_ok = getattr(self, "_sonnet_available", True)
            haiku_players = []
            sonnet_players = []
            for p in context["players"]:
                if sonnet_ok and needs_sonnet_reasoning(p):
                    sonnet_players.append(p)
                else:
                    haiku_players.append(p)

            logger.info(
                "%s: %d haiku batch, %d sonnet individual%s",
                team, len(haiku_players), len(sonnet_players),
                " (Sonnet unavailable — all via Haiku)" if not sonnet_ok else "",
            )

            all_profiles: list[dict] = []

            # Pass 1: Haiku team batch (stable veterans)
            if haiku_players:
                haiku_context = {**context, "players": haiku_players}
                raw = await self.call_once(
                    system=HAIKU_SYSTEM_PROMPT,
                    user=(
                        f"Build player profiles for the {team} skill-position players "
                        f"using this pre-aggregated data:\n\n"
                        f"{json.dumps(haiku_context, default=str)}"
                    ),
                    input_data=haiku_context,
                    entity_id=f"{team}_haiku",
                )
                if raw:
                    try:
                        profiles = parse_json_output(raw)
                        if isinstance(profiles, dict):
                            profiles = [profiles]
                        if isinstance(profiles, list):
                            all_profiles.extend(profiles)
                    except (json.JSONDecodeError, ValueError) as exc:
                        logger.warning(
                            "%s: Haiku batch JSON parse failed (%s), "
                            "continuing with Sonnet-only profiles",
                            team, exc,
                        )

            # Pass 2: Sonnet per-player (complex players)
            for player in sonnet_players:
                is_rookie_player = bool(player.get("is_rookie"))

                # Rookies with comp data get a dedicated rookie prompt
                has_comps = bool(
                    player.get("historical_comp_names")
                    or player.get("comp_yr1_avg_ppg")
                )

                if is_rookie_player and has_comps:
                    # Rookie Sonnet path: college comp baseline + AI reasoning
                    player["_team"] = team  # attach team for prompt formatting
                    rookie_user = _format_rookie_prompt(
                        player, context.get("team_system", {})
                    )
                    player_context = {
                        "team": team,
                        "analysis_year": context["analysis_year"],
                        "team_system": context["team_system"],
                        "player": player,
                        "_rookie_sonnet": True,
                    }
                    try:
                        raw = await self._call_sonnet_with_limit(
                            system=ROOKIE_SONNET_SYSTEM,
                            user=rookie_user,
                            input_data=player_context,
                            entity_id=f"{team}_{player['name']}_rookie",
                            max_tokens=800,
                        )
                        if raw:
                            parsed = _parse_rookie_sonnet_output(raw, player)
                            if parsed:
                                # Tag as rookie Sonnet output so _write_profiles
                                # knows to merge with Python baseline
                                parsed["player_name"] = player["name"]
                                parsed["_rookie_sonnet"] = True
                                all_profiles.append(parsed)
                            else:
                                # Parse failed — use fallback
                                fb = _rookie_sonnet_fallback(player)
                                fb["player_name"] = player["name"]
                                fb["_rookie_sonnet"] = True
                                all_profiles.append(fb)
                        else:
                            # Cache hit returned empty — use fallback
                            fb = _rookie_sonnet_fallback(player)
                            fb["player_name"] = player["name"]
                            fb["_rookie_sonnet"] = True
                            all_profiles.append(fb)
                    except Exception as exc:
                        logger.warning(
                            "%s: Rookie Sonnet call failed for %s (%s), using fallback",
                            team, player["name"], exc,
                        )
                        fb = _rookie_sonnet_fallback(player)
                        fb["player_name"] = player["name"]
                        fb["_rookie_sonnet"] = True
                        all_profiles.append(fb)
                else:
                    # Veteran Sonnet path (or rookie without comps → handled
                    # in _write_profiles as Python-only)
                    player_context = {
                        "team": team,
                        "analysis_year": context["analysis_year"],
                        "team_system": context["team_system"],
                        "player": player,
                    }
                    try:
                        raw = await self._call_sonnet_with_limit(
                            system=SONNET_SYSTEM_PROMPT,
                            user=(
                                f"Project PPR for {player['name']} ({team}):\n\n"
                                f"{json.dumps(player_context, default=str)}"
                            ),
                            input_data=player_context,
                            entity_id=f"{team}_{player['name']}",
                        )
                        if raw:
                            prof = parse_json_output(raw)
                            if isinstance(prof, list) and prof:
                                prof = prof[0]
                            if isinstance(prof, dict):
                                all_profiles.append(prof)
                    except Exception as exc:
                        logger.warning(
                            "%s: Sonnet call failed for %s (%s), skipping player",
                            team, player["name"], exc,
                        )

            written = await _write_profiles(
                all_profiles, context, team,
                stale_names=stale_names,
                depth_players=context.get("depth_players", []),
            )
            logger.info("%s: %d profiles written", team, written)
            return written

        except Exception as exc:
            logger.error("Player Profiles Agent failed for %s: %s", team, exc, exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # Full pipeline — pre-warm caches once, then run all 32 teams
    # ------------------------------------------------------------------

    async def run_all_teams(
        self, warehouse=None, concurrency: int = 10, force: bool = False,
    ) -> dict[str, int]:
        """
        Run all 32 teams. Warehouse provides all NFL data — no pre-loading needed.
        Returns {team_abbr: profiles_written}.

        Default concurrency=10 with _sonnet_semaphore=6 limiting Sonnet burst.
        Combined with _sonnet_semaphore(2), this caps peak Sonnet calls to 2.
        """
        if warehouse is not None:
            self._warehouse = warehouse

        # Pre-flight: check if Sonnet is reachable
        sonnet_ok = await self._check_sonnet_available()
        self._sonnet_available = sonnet_ok

        logger.info(
            "Starting Player Profiles pipeline (concurrency=%d, sonnet=%s)",
            concurrency, "available" if sonnet_ok else "UNAVAILABLE — Haiku fallback",
        )
        semaphore = asyncio.Semaphore(concurrency)
        results: dict[str, int] = {}

        async def _run_one(team: str) -> None:
            async with semaphore:
                results[team] = await self.run_for_team(team, force=force)

        await asyncio.gather(*[_run_one(t) for t in NFL_TEAMS])

        total = sum(results.values())
        logger.info("Player Profiles pipeline complete: %d total profiles written", total)
        if not sonnet_ok:
            logger.warning(
                "Sonnet was unavailable — complex players used Haiku fallback. "
                "Re-run when Sonnet is back for full-quality profiles."
            )
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
            if not match:
                # No DB player on this team shares this last name.
                # Do NOT fall back to another team's player — that causes cross-team
                # profile writes (e.g. "Skyy Moore" for BUF resolving to KC's Skyy Moore).
                results[(name, team)] = None
            elif len(match) == 1:
                results[(name, team)] = str(match[0].id)
            else:
                # Multiple players on same team share a last name (e.g. Tyreek Hill vs Julian Hill).
                # Use full first name to pick the right record — first initial alone fails
                # when two same-team players share it (e.g. Carlos vs Casey Washington).
                first = name.split()[0].lower() if name else ""
                first_match = [
                    p for p in match
                    if p.name and p.name.split()[0].lower() == first
                ]
                results[(name, team)] = str((first_match or match)[0].id)

    return results


async def _write_profiles(
    profiles: list[dict], context: dict, team: str,
    stale_names: set[str] | None = None,
    depth_players: list[dict] | None = None,
) -> int:
    """Write player_profiles for one team.

    When stale_names is provided, only deletes profiles for those players (selective refresh).
    When stale_names is None, deletes all team profiles before re-inserting (force/first run).
    """
    if not profiles and not depth_players:
        return 0

    analysis_year = get_analysis_year()
    ctx_players   = context.get("players", [])

    # Build two lookups for finding the context player from model output:
    #   1. exact name  → ctx player dict
    #   2. normalized name → ctx player dict  (handles D.K./DK, Ja'Marr/Jamarr)
    ctx_by_name: dict[str, dict] = {p["name"]: p for p in ctx_players}
    ctx_by_norm: dict[str, dict] = {
        normalize_player_name(p["name"]): p for p in ctx_players
    }

    async with AsyncSessionLocal() as session:
        # Get all players for this team from DB to build ID resolution maps.
        team_players = (
            await session.execute(select(Player).where(Player.team_abbr == team))
        ).scalars().all()

        # Map nfl gsis player_id → DB uuid (most reliable cross-source key)
        nfl_id_to_db: dict[str, str] = {}
        for p in team_players:
            if p.yahoo_player_id and p.yahoo_player_id.startswith("nfl_"):
                nfl_id = p.yahoo_player_id[4:]  # strip "nfl_" prefix
                nfl_id_to_db[nfl_id] = str(p.id)

        # Map normalized name → DB uuid (fallback when no nfl_player_id available)
        name_to_db = build_player_lookup(
            [{"name": p.name, "id": str(p.id)} for p in team_players]
        )

        # Delete profiles — selective (cache-aware) or full team wipe (force/first run)
        if stale_names is not None:
            stale_ids = [p.id for p in team_players if p.name in stale_names]
            if stale_ids:
                existing = (
                    await session.execute(
                        select(PlayerProfile).where(
                            PlayerProfile.player_id.in_(stale_ids)
                        )
                    )
                ).scalars().all()
                for ep in existing:
                    await session.delete(ep)
                if existing:
                    logger.debug("%s: deleted %d stale profile(s) for refresh", team, len(existing))
        else:
            team_player_ids = [p.id for p in team_players]
            if team_player_ids:
                existing = (
                    await session.execute(
                        select(PlayerProfile).where(
                            PlayerProfile.player_id.in_(team_player_ids)
                        )
                    )
                ).scalars().all()
                for ep in existing:
                    await session.delete(ep)
                if existing:
                    logger.debug("%s: deleted %d stale profile(s) before rewrite", team, len(existing))

        written = 0
        written_ids: set = set()  # deduplicate: only first profile per player_id
        for prof in profiles:
            pname = prof.get("player_name", "")

            # --- Resolve context player (hallucination guard) ---
            ctx_player = ctx_by_name.get(pname) or ctx_by_norm.get(normalize_player_name(pname))
            if not ctx_player:
                logger.debug("Hallucinated (not in context): %s (%s)", pname, team)
                continue

            # --- Resolve DB UUID ---
            player_id: str | None = None
            # Primary: nfl gsis player_id passed through from roster
            nfl_pid = ctx_player.get("nfl_player_id")
            if nfl_pid:
                player_id = nfl_id_to_db.get(nfl_pid)
            # Fallback: normalized name lookup within this team's DB records
            if not player_id:
                player_id = name_to_db.get(normalize_player_name(pname))
            if not player_id:
                logger.debug("Could not resolve DB ID: %s (%s)", pname, team)
                continue

            if player_id in written_ids:
                logger.debug("Duplicate profile skipped: %s (%s)", pname, team)
                continue

            seasons    = ctx_player.get("seasons", [])
            ts3yr, ts_last, ay3yr = _compute_season_averages(seasons, analysis_year)

            # Route: rookies → Python + optional Sonnet, veterans → AI/Python
            is_rookie = bool(ctx_player.get("is_rookie", False))
            has_rookie_sonnet = bool(prof.get("_rookie_sonnet"))
            has_ai_projection = bool(prof.get("projected_ppr_points"))

            if is_rookie:
                # Step 1: Python baseline from college comps (always computed)
                team_ctx = context.get("team_system", {})
                rookie_prof = _build_rookie_profile(ctx_player, team_ctx)

                if has_rookie_sonnet and prof.get("projected_ppr_season"):
                    # Step 2: Merge Sonnet reasoning with Python baseline
                    clean_baseline = {
                        **rookie_prof["clean_season_baseline"],
                        "projected_ppr_season": round(float(prof["projected_ppr_season"]), 1),
                        "projected_ppr_floor": round(float(prof.get("projected_ppr_floor", 0)), 1),
                        "projected_ppr_ceiling": round(float(prof.get("projected_ppr_ceiling", 0)), 1),
                        "breakout_probability": round(float(prof.get("breakout_probability", 0.1)), 2),
                        "comp_translation_grade": prof.get("comp_translation_grade", "C"),
                        "key_risks": prof.get("key_risks", []),
                        "key_upside_factors": prof.get("key_upside_factors", []),
                    }
                    # Override Python ceiling/floor with Sonnet's wider range
                    rookie_prof["ceiling_value_ppr"] = round(float(prof.get("projected_ppr_ceiling", rookie_prof["ceiling_value_ppr"])), 1)
                    rookie_prof["floor_value_ppr"] = round(float(prof.get("projected_ppr_floor", rookie_prof["floor_value_ppr"])), 1)
                    # Sonnet drives role classification for rookies
                    rookie_prof["role_classification"] = prof.get("role_classification") or rookie_prof.get("role_classification")
                    rookie_prof["profile_source"] = "sonnet_rookie"
                    rookie_prof["confidence"] = prof.get("confidence", "low")
                else:
                    # No Sonnet — Python-only path (no comps or Sonnet failed)
                    clean_baseline = rookie_prof["clean_season_baseline"]
            elif has_ai_projection:
                # Sonnet-profiled player: AI drives the PPR projection
                ai_ppr = float(prof["projected_ppr_points"])
                rookie_prof = {}

                # Compute Python baseline for sanity-checking and stat components
                if ctx_player.get("position") == "QB":
                    python_baseline = _compute_qb_baseline(seasons)
                else:
                    python_baseline = _compute_clean_baseline(seasons)

                python_ppr = python_baseline.get("ppr_points", 0) if python_baseline else 0

                # Sanity guard: log if AI diverges > 50% from Python baseline
                if python_ppr > 0 and abs(ai_ppr - python_ppr) / python_ppr > 0.50:
                    logger.warning(
                        "AI projection %.1f diverges >50%% from baseline %.1f for %s (%s)",
                        ai_ppr, python_ppr, pname, team,
                    )

                clean_baseline = python_baseline if python_baseline else {}
                # Keep historical ppr_points from Python baseline unchanged;
                # store Sonnet's forward projection as projected_ppr_season
                clean_baseline["projected_ppr_season"] = round(ai_ppr, 1)
                if prof.get("upside_ppr"):
                    clean_baseline["upside_ppr"] = round(float(prof["upside_ppr"]), 1)
                if prof.get("downside_ppr"):
                    clean_baseline["downside_ppr"] = round(float(prof["downside_ppr"]), 1)
            elif ctx_player.get("position") == "QB":
                # QB baseline uses fantasy_points_ppr (includes passing scoring)
                clean_baseline = _compute_qb_baseline(seasons)
                rookie_prof    = {}
            else:
                # WR/RB/TE Haiku batch: compute clean_season_baseline in Python
                # PPR formula: receptions×1 + (rec_yards+rush_yards)×0.1 + tds×6
                clean_baseline = _compute_clean_baseline(seasons)
                rookie_prof    = {}

            # Upsert: use existing record if already written by another team batch
            existing_record = (
                await session.execute(
                    select(PlayerProfile).where(PlayerProfile.player_id == player_id)
                )
            ).scalar_one_or_none()
            if existing_record:
                record = existing_record
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
            if clean_baseline is None:
                clean_baseline = {}
            clean_baseline["prompt_version"] = PLAYER_PROFILES_PROMPT_VERSION
            record.clean_season_baseline      = clean_baseline
            record.anomalous_seasons_excluded = effective.get("anomalous_seasons_excluded") or []
            record.breakout_flag              = bool(effective.get("breakout_flag", False))
            record.breakout_reasoning         = effective.get("breakout_reasoning")
            record.projection_reasoning       = (
                prof.get("projection_reasoning") if (has_ai_projection or has_rookie_sonnet) else None
            )
            record.positional_scarcity_tier   = effective.get("positional_scarcity_tier")
            record.target_share_3yr_avg       = _to_decimal(ts3yr)
            record.target_share_last_season   = _to_decimal(ts_last)
            record.air_yards_share            = _to_decimal(ay3yr)
            record.snap_percentage            = _to_decimal(ctx_player.get("snap_pct"))

            # Rookie-specific columns
            record.is_rookie        = is_rookie
            record.profile_source   = (
                rookie_prof.get("profile_source", "college_comps") if is_rookie
                else ("sonnet_projection" if has_ai_projection else "nfl_history")
            )
            record.confidence       = (
                rookie_prof.get("confidence", "low") if is_rookie
                else prof.get("confidence", "medium") if has_ai_projection
                else "medium"
            )
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

            written_ids.add(player_id)
            written += 1

        # --- Second pass: QBs not in model output get Python-only profiles ---
        for ctx_player in ctx_players:
            if ctx_player.get("position") != "QB":
                continue
            pname = ctx_player["name"]
            nfl_pid = ctx_player.get("nfl_player_id")

            # Resolve DB ID (same logic as above)
            player_id: str | None = None
            if nfl_pid:
                player_id = nfl_id_to_db.get(nfl_pid)
            if not player_id:
                player_id = name_to_db.get(normalize_player_name(pname))
            if not player_id or player_id in written_ids:
                continue  # already written above or can't resolve

            seasons = ctx_player.get("seasons", [])
            clean_baseline = _compute_qb_baseline(seasons)
            if not clean_baseline:
                continue  # not enough data

            record = PlayerProfile(player_id=player_id, season_year=analysis_year)
            session.add(record)
            record.role_classification = _derive_qb_role(clean_baseline)
            clean_baseline["prompt_version"] = PLAYER_PROFILES_PROMPT_VERSION
            record.clean_season_baseline = clean_baseline
            record.anomalous_seasons_excluded = []
            record.is_rookie = bool(ctx_player.get("is_rookie", False))
            record.profile_source = "nfl_history"
            record.confidence = "medium"
            record.age_curve_position = _derive_qb_age_curve(ctx_player.get("age"))
            record.efficiency_signal = _derive_qb_efficiency(clean_baseline)

            written_ids.add(player_id)
            written += 1
            logger.debug("QB Python-only profile: %s (%s)", pname, team)

        # --- Third pass: depth players with no stats in any season ---
        for dp in (depth_players or []):
            pname = dp["name"]
            nfl_pid = dp.get("nfl_player_id")

            player_id: str | None = None
            if nfl_pid:
                player_id = nfl_id_to_db.get(nfl_pid)
            if not player_id:
                player_id = name_to_db.get(normalize_player_name(pname))
            if not player_id or player_id in written_ids:
                continue

            depth = _build_depth_profile(dp["position"])
            record = PlayerProfile(player_id=player_id, season_year=analysis_year)
            session.add(record)
            record.role_classification = depth["role_classification"]
            record.clean_season_baseline = {"prompt_version": PLAYER_PROFILES_PROMPT_VERSION}
            record.anomalous_seasons_excluded = []
            record.is_rookie = False
            record.profile_source = "nfl_history"
            record.confidence = depth["confidence"]
            record.efficiency_signal = depth["efficiency_signal"]
            record.positional_scarcity_tier = depth["positional_scarcity_tier"]

            written_ids.add(player_id)
            written += 1
            logger.debug("Depth profile: %s (%s)", pname, team)

        await session.commit()

    return written


def _derive_qb_role(baseline: dict) -> str:
    """Derive QB role classification from PPG baseline."""
    ppg = baseline.get("ppg", 0)
    if ppg >= 22:
        return "qb_elite"
    if ppg >= 18:
        return "qb_starter"
    if ppg >= 14:
        return "qb_streamer"
    return "qb_backup"


def _derive_qb_age_curve(age: int | None) -> str:
    """Derive age curve position for QBs (peak 26-32)."""
    if age is None:
        return "peak"
    if age < 26:
        return "ascending"
    if age <= 32:
        return "peak"
    return "descending"


def _derive_qb_efficiency(baseline: dict) -> str:
    """Derive efficiency signal from QB PPG."""
    ppg = baseline.get("ppg", 0)
    if ppg >= 22:
        return "elite"
    if ppg >= 18:
        return "above_avg"
    if ppg >= 14:
        return "average"
    return "below_avg"


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


_MINIMUM_TOUCHES_FOR_PROJECTION = 50  # career receptions + carries across all seasons
_MINIMUM_QB_GAMES = 10  # minimum career games for QB projection

# Recency weights: most recent season weighted most heavily
_RECENCY_WEIGHTS = {0: 0.50, 1: 0.30, 2: 0.20}
# 0 = most recent, 1 = one year ago, 2 = two years ago
# Older than 2 years back gets 10% each


def _compute_weighted_baseline(
    season_stats: dict[int, float],
    injury_shortened: set[int],
) -> float:
    """
    Compute PPR baseline weighted toward recent seasons.
    Excludes injury-shortened seasons (< 10 games).

    Args:
        season_stats: {season_year: ppr_total}
        injury_shortened: set of seasons with < 10 games

    Returns:
        weighted average PPR
    """
    # Filter out injury-shortened seasons
    clean_seasons = {
        yr: ppr for yr, ppr in season_stats.items()
        if yr not in injury_shortened
    }

    if not clean_seasons:
        # All seasons injury-shortened — use best available
        clean_seasons = season_stats

    if not clean_seasons:
        return 0.0

    # Sort by year descending (most recent first)
    sorted_seasons = sorted(
        clean_seasons.items(),
        key=lambda x: x[0],
        reverse=True,
    )

    # Assign weights
    total_weight = 0.0
    weighted_sum = 0.0

    for i, (year, ppr) in enumerate(sorted_seasons):
        weight = _RECENCY_WEIGHTS.get(i, 0.10)
        weighted_sum += ppr * weight
        total_weight += weight

    if total_weight == 0:
        return 0.0

    # Normalize in case not all 3 seasons available
    return weighted_sum / total_weight


def _compute_qb_baseline(seasons: list[dict]) -> dict:
    """
    Compute clean_season_baseline for QBs using fantasy_points_ppr directly.

    QB PPR scoring includes passing (which the rec/rush formula doesn't capture):
        passing_td × 4 + passing_yards × 0.04 + INT × -2 +
        rushing_yards × 0.1 + rushing_td × 6 + receptions × 1

    Clean season = games >= 10.
    Minimum threshold: 10+ career games across all seasons.
    Career decline detection: same 65% rule as skill positions.
    """
    total_games = sum(s.get("games", 0) for s in seasons)
    if total_games < _MINIMUM_QB_GAMES:
        return {}

    clean = [s for s in seasons if s.get("games", 0) >= 10]
    if not clean:
        clean = [s for s in seasons if s.get("games", 0) > 0]
    if not clean:
        return {}

    def _season_ppg(s: dict) -> float:
        fp = s.get("fantasy_points_ppr") or s.get("ppr_per_game", 0)
        games = s.get("games", 1)
        if s.get("ppr_per_game"):
            return float(s["ppr_per_game"])
        return float(fp) / games if games > 0 else 0.0

    # Career decline detection
    sorted_clean = sorted(clean, key=lambda s: s.get("year", 0))
    season_ppgs = [_season_ppg(s) for s in sorted_clean]
    peak_ppg = max(season_ppgs) if season_ppgs else 0
    recent_ppg = season_ppgs[-1] if season_ppgs else 0

    is_declining = peak_ppg > 0 and recent_ppg < peak_ppg * 0.65

    if is_declining and len(sorted_clean) >= 2:
        career_ppg = sum(season_ppgs) / len(season_ppgs)
        avg_ppg = recent_ppg * 0.6 + career_ppg * 0.4
    else:
        # Recency-weighted average: most recent season counts most
        # Use _compute_weighted_baseline on season PPR totals, then convert to PPG
        injury_shortened = {
            s.get("year") for s in seasons if 0 < s.get("games", 0) < 10
        }
        season_ppr_totals = {}
        for s in sorted_clean:
            yr = s.get("year", 0)
            ppg = _season_ppg(s)
            games = s.get("games", 1)
            season_ppr_totals[yr] = ppg * games if games > 0 else 0.0
        weighted_total = _compute_weighted_baseline(season_ppr_totals, injury_shortened)
        # Convert back to PPG using average games from clean seasons
        avg_games = sum(s.get("games", 1) for s in sorted_clean) / len(sorted_clean)
        avg_ppg = weighted_total / avg_games if avg_games > 0 else 0.0

    ppr_points = round(avg_ppg * 17, 1)  # 17-game projection

    # Passing stats averages
    pass_yds_pg = sum(
        s.get("passing_yards", 0) / max(s.get("games", 1), 1) for s in clean
    ) / len(clean)
    pass_tds_pg = sum(
        s.get("passing_tds", 0) / max(s.get("games", 1), 1) for s in clean
    ) / len(clean)
    avg_cpoe = None
    cpoe_vals = [s.get("cpoe") for s in clean if s.get("cpoe") is not None]
    if cpoe_vals:
        avg_cpoe = round(sum(cpoe_vals) / len(cpoe_vals), 2)

    result = {
        "ppr_points": ppr_points,
        "ppg": round(avg_ppg, 1),
        "passing_yards_pg": round(pass_yds_pg, 1),
        "passing_tds_pg": round(pass_tds_pg, 2),
    }
    if avg_cpoe is not None:
        result["cpoe"] = avg_cpoe
    if is_declining:
        result["declining"] = True
    return result


def _compute_clean_baseline(seasons: list[dict]) -> dict:
    """
    Compute clean_season_baseline with recency weighting across clean seasons.

    Clean season = games >= 10 AND NOT backup_qb_season.
    Falls back to all seasons with games > 0 if no clean seasons exist.

    Recency weighting (most recent first): 50%, 30%, 20%.
    Older seasons get 10% each. Weights are normalized if fewer
    seasons are available.

    Minimum usage threshold: player must have at least 50 career touches
    (receptions + carries) to receive a projection. This prevents low-usage
    players (e.g. Jermar Jefferson with 21 career attempts) from getting
    inflated baselines.

    Career decline detection: if the most recent season's PPR is below 65%
    of the career peak, weight recent season at 60% and career average at 40%.
    This prevents aging/injured players (e.g. Chubb) from projecting at
    their peak. Takes priority over recency weighting when triggered.

    PPR formula (LEAGUE_RULES.md Rule #7):
        ppr_points = receptions × 1 + (rec_yards + rush_yards) × 0.1 + (rec_tds + rush_tds) × 6
    """
    # --- Minimum usage threshold ---
    total_receptions = sum(s.get("receptions", 0) for s in seasons)
    total_carries = sum(s.get("carries", 0) for s in seasons)
    if total_receptions + total_carries < _MINIMUM_TOUCHES_FOR_PROJECTION:
        return {}

    clean = [
        s for s in seasons
        if s.get("games", 0) >= 10 and not s.get("backup_qb_season", False)
    ]
    if not clean:
        clean = [s for s in seasons if s.get("games", 0) > 0]
    if not clean:
        return {}

    def _season_ppr(s: dict) -> float:
        rec = s.get("receptions", 0)
        rec_yds = s.get("rec_yards", 0)
        rec_td = s.get("rec_tds", 0)
        rush_yds = s.get("rush_yards", 0)
        rush_td = s.get("rush_tds", 0)
        return rec * 1.0 + (rec_yds + rush_yds) * 0.1 + (rec_td + rush_td) * 6.0

    # --- Career decline detection ---
    # Sort by year (most recent last) to identify recent vs peak
    sorted_clean = sorted(clean, key=lambda s: s.get("year", 0))
    season_pprs = [_season_ppr(s) for s in sorted_clean]
    peak_ppr = max(season_pprs) if season_pprs else 0
    recent_ppr = season_pprs[-1] if season_pprs else 0

    is_declining = peak_ppr > 0 and recent_ppr < peak_ppr * 0.65

    if is_declining and len(sorted_clean) >= 2:
        # Weight recent season 60%, career average 40%
        recent = sorted_clean[-1]
        career_n = len(sorted_clean)
        career_rec = sum(s.get("receptions", 0) for s in sorted_clean) / career_n
        career_rec_yards = sum(s.get("rec_yards", 0) for s in sorted_clean) / career_n
        career_rec_tds = sum(s.get("rec_tds", 0) for s in sorted_clean) / career_n
        career_rush_yards = sum(s.get("rush_yards", 0) for s in sorted_clean) / career_n
        career_rush_tds = sum(s.get("rush_tds", 0) for s in sorted_clean) / career_n

        rec = recent.get("receptions", 0) * 0.6 + career_rec * 0.4
        rec_yards = recent.get("rec_yards", 0) * 0.6 + career_rec_yards * 0.4
        rec_tds = recent.get("rec_tds", 0) * 0.6 + career_rec_tds * 0.4
        rush_yards = recent.get("rush_yards", 0) * 0.6 + career_rush_yards * 0.4
        rush_tds = recent.get("rush_tds", 0) * 0.6 + career_rush_tds * 0.4
    else:
        # Recency-weighted average: most recent season counts most
        # Sort descending by year (most recent first) for weight assignment
        desc_clean = sorted(sorted_clean, key=lambda s: s.get("year", 0), reverse=True)
        total_weight = 0.0
        rec = rec_yards = rec_tds = rush_yards = rush_tds = 0.0
        for i, s in enumerate(desc_clean):
            w = _RECENCY_WEIGHTS.get(i, 0.10)
            rec += s.get("receptions", 0) * w
            rec_yards += s.get("rec_yards", 0) * w
            rec_tds += s.get("rec_tds", 0) * w
            rush_yards += s.get("rush_yards", 0) * w
            rush_tds += s.get("rush_tds", 0) * w
            total_weight += w
        if total_weight > 0:
            rec /= total_weight
            rec_yards /= total_weight
            rec_tds /= total_weight
            rush_yards /= total_weight
            rush_tds /= total_weight

    yards = rec_yards + rush_yards
    tds   = rec_tds + rush_tds
    ppr   = rec * 1.0 + yards * 0.1 + tds * 6.0

    result = {
        "receptions":  round(rec, 1),
        "yards":       round(yards, 1),
        "touchdowns":  round(tds, 1),
        "ppr_points":  round(ppr, 1),
    }
    if is_declining:
        result["declining"] = True
    return result


def _build_depth_profile(position: str) -> dict:
    """
    Build a minimal profile for fringe/depth players with no stats in any
    analysis season.  Returns the same shape as model output so the DB write
    path works unchanged.

    These players had zero games across all analysis seasons *after* the
    position-aware name collision fix, meaning they genuinely have no NFL
    production.  Rather than sending empty data to the AI model (which
    produces hallucinated profiles), we assign conservative depth defaults.
    """
    return {
        "role_classification": "depth",
        "efficiency_signal": "below_average",
        "age_curve_position": "unknown",
        "career_trajectory": "unknown",
        "breakout_flag": False,
        "positional_scarcity_tier": "deep",
        "confidence": "low",
        "clean_season_baseline": {},  # no baseline — no stats
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


def _get_agent(dry_run: bool = False, warehouse=None) -> PlayerProfilesAgent:
    global _agent_instance
    if _agent_instance is None or _agent_instance.dry_run != dry_run:
        _agent_instance = PlayerProfilesAgent(dry_run=dry_run, warehouse=warehouse)
    elif warehouse is not None:
        _agent_instance._warehouse = warehouse
    return _agent_instance


async def run_for_team(team_abbr: str, dry_run: bool = False, force: bool = False) -> int:
    return await _get_agent(dry_run).run_for_team(team_abbr, force=force)


async def run_all_teams(
    concurrency: int = 10, dry_run: bool = False, force: bool = False, warehouse=None,
) -> dict[str, int]:
    return await _get_agent(dry_run, warehouse=warehouse).run_all_teams(
        warehouse=warehouse, concurrency=concurrency, force=force,
    )
