"""
Valuation Agent — AI Ceiling Calibration

Applies AI strategic reasoning to math-derived bid ceilings. Runs AFTER
the valuation pass (step 7) and produces auction intelligence:
  - AI-adjusted bid ceiling with confidence range
  - Value assessment (elite_value / good_value / fair_value / slight_overpay / avoid)
  - Auction tactical notes
  - PAY UP flag (undervalued players to aggressively pursue)
  - NOMINATION TARGET flag (overvalued players to nominate early to drain opponents)

Architecture:
  - Tier-based batching (not team-based):
    - Tier 1: individual Sonnet calls (~15-20 calls)
    - Tier 2-3: batches of 5-6 per Sonnet call (~15-20 calls)
    - Tier 4-5: batches of 10 per Haiku call (~10-15 calls)
  - All data pre-loaded in one query — no per-player DB queries
  - Pattern: pre-aggregate in Python → batch by tier → parse JSON → write DB
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.agents.base_agent import BaseAgent, parse_json_output, HAIKU, SONNET
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile, PlayerSchedule
from backend.models.dependency import PlayerDependency
from backend.utils.seasons import get_current_season


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snake-ADP clamps (pick numbers, 1-200; LOWER = earlier = more valuable —
# the OPPOSITE of the dollar bid-ceiling maxes). QB/K/DEF floor late because
# they go far later in snake than their auction dollars imply (e.g. Lamar
# Jackson ~$38 auction but pick ~35-40 in snake PPR).
ADP_POSITION_RANGES: dict[str, tuple[int, int]] = {
    "RB":  (1,   100),
    "WR":  (1,   120),
    "QB":  (25,  170),   # floor 25 so elite QBs (Allen) aren't clamped too late;
                         # cap 170 so streaming QBs still get drafted, not skipped
    "TE":  (10,  150),
    "K":   (140, 200),   # kickers always last
    "DEF": (130, 200),   # defenses always last
}

# Scoring format the valuation agent values for. Its prompt is PPR (see below),
# so adp_ai / adp_scoring are stamped "ppr" — kept consistent with sync_adp's
# default so the adp_scoring column means the same thing whichever step wrote it.
# Becomes configurable when half-PPR support lands (Stage 30).
VALUATION_SCORING = "ppr"


def clamp_adp(adp_ai, position: str | None) -> float | None:
    """Clamp the model's snake ADP into the position's valid pick range.

    LOWER = earlier pick = more valuable (the inverse of the bid-ceiling clamp),
    so an over-eager QB at pick 5 is pushed back to the position floor (50).
    """
    if adp_ai is None:
        return None
    lo, hi = ADP_POSITION_RANGES.get(position or "", (1, 200))
    return max(lo, min(round(float(adp_ai), 1), hi))


# Bump to invalidate the valuation_agent cache (it keys on input_data, so the
# version is folded into the key). Increment on any SYSTEM_PROMPT change.
VALUATION_AGENT_VERSION = "v2"

# Position-relative "strong production" PPR season totals — used to split a
# we-rate-earlier player into VALUE (production justifies it) vs SLEEPER
# (upside exceeds cost, but modest production for the position).
_STRONG_PPR = {"QB": 320, "RB": 240, "WR": 240, "TE": 170}

# Beyond this pick depth (12 teams x 15 rounds) ADP comparisons aren't
# meaningful: our adp_rank runs 1..N (~640) while FantasyPros' overall rank only
# runs 1..~410, so the two rank lists diverge in length and deep players get
# nonsense diffs (e.g. -350). Past the window we neutralize the flag to TARGET.
DRAFTABLE_WINDOW = 180


def compute_adp_diff(adp_fantasypros, adp_rank):
    """FP rank minus our rank — both clean 1-N integer ranks.

    adp_fantasypros is FantasyPros' overall rank; adp_rank is our 1-N ordering
    (the value shown as "AI ADP" on the board). Computing against adp_rank (not
    adp_ai) keeps the board's DIFF consistent with both displayed columns —
    adp_ai has heavy ties (Bijan/Gibbs/Chase all 4.0) that made the diff
    disagree with the rank shown next to it.

    Positive => FP ranks the player LATER than we do (we like them more —
    potential value). Negative => market values them above us (potential reach).
    """
    if adp_fantasypros is None or adp_rank is None:
        return None
    return float(adp_fantasypros) - float(adp_rank)


def assign_adp_ranks(players) -> int:
    """Assign adp_rank = 1..N to players already sorted by adp_ai ascending.

    Mutates each player; returns the count ranked.
    """
    for rank, p in enumerate(players, start=1):
        p.adp_rank = rank
    return len(players)


def classify_snake_flag(adp_diff, projected_ppr, position: str | None, adp_rank=None) -> str:
    """Deterministic snake_flag from the ADP differential + production.

    This is the SOLE source of snake_flag. The model is not asked for it: adp_diff
    is computed from the model's own adp_ai output, so the model cannot know it at
    inference time (it would have to guess blind).

      VALUE   — adp_diff >= 15 AND strong production for the position
      SLEEPER — adp_diff >= 15 BUT modest production (upside > cost, late pick)
      TARGET  — |adp_diff| < 15 (we agree with consensus), or no FP ADP, or the
                player is beyond the draftable window (diff is rank-scale noise)
      REACH   — adp_diff <= -15 (market values them well above us)
    """
    # Past the draftable window the rank-list lengths diverge, so the diff is
    # noise — neutralize to TARGET even though the raw adp_diff is still stored.
    if adp_rank is not None and adp_rank > DRAFTABLE_WINDOW:
        return "TARGET"
    if adp_diff is None:
        return "TARGET"
    diff = float(adp_diff)
    if diff <= -15:
        return "REACH"
    if diff >= 15:
        strong = projected_ppr is not None and float(projected_ppr) >= _STRONG_PPR.get(
            position or "", 240
        )
        return "VALUE" if strong else "SLEEPER"
    return "TARGET"

# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an auction draft strategist for a 12-team PPR fantasy football league.
Each team has a $200 budget. The target is $185 on 7 starting skill players (QB, 2×RB, 2×WR, TE, FLEX).

You receive players with their MATH-DERIVED bid ceilings (from a PAR valuation engine) plus
full scouting context. Your job is to apply STRATEGIC JUDGMENT to calibrate the final bid ceiling.

REQUIRED OUTPUT — EVERY field below must be present in EVERY object (never omit
adp_ai, even for elite players):
{
  "player_name": "string — must match input exactly",
  "adp_ai": number — REQUIRED, never null. Snake-draft pick number (1-200, LOWER = earlier). SEE THE SNAKE DRAFT ADP SECTION BELOW,
  "ai_bid_ceiling": integer — your recommended max bid ($1 minimum),
  "confidence_floor": integer — lowest you'd bid in a cautious room,
  "confidence_ceiling": integer — highest you'd go in an aggressive room,
  "value_assessment": "string — one of: elite_value, good_value, fair_value, slight_overpay, avoid",
  "auction_note": "string — 1-2 sentences on the player's fantasy OUTLOOK: role, usage, production potential. Do NOT mention dollar amounts, bid prices, or auction strategy — this note appears in both auction AND snake contexts",
  "pay_up_flag": boolean — true if this player is clearly undervalued and worth paying above math ceiling,
  "nomination_target_flag": boolean — true if this player is overvalued and should be nominated early to drain opponent budgets
}

You may also receive market context:
- market_value_fantasypros: consensus ADP price from FantasyPros
- prior_season_price: what this player actually sold for in prior season auctions
Use these as references for what the market typically pays for this player.
When both are available, note any gap — a player whose ADP is $30 but sold for $45 last year
likely faces aggressive bidding again.

Rules:
- ai_bid_ceiling should usually be within 15-20% of the math ceiling, but you CAN deviate more with reasoning
- confidence_floor must be < ai_bid_ceiling, confidence_ceiling must be > ai_bid_ceiling
- pay_up_flag = true means: "If someone else bids near your ceiling, keep going — this player is special"
- nomination_target_flag = true means: "Nominate this player early — opponents will overpay, draining their budget"
- auction_note must reference the player's specific situation (role / usage /
  production), not generic advice, and contain NO dollar amounts or bid prices
- Use market context (consensus ADP, prior price) to inform ai_bid_ceiling and
  value_assessment — but keep dollar figures OUT of auction_note
- NEVER say "your league paid" or "in your league" — this analysis is shared across all users
- Value assessment considers: projection confidence, injury risk, schedule, dependency flags, positional scarcity
- Max realistic bids: RB=$80, WR=$70, QB=$50, TE=$45. Never exceed these.

SNAKE DRAFT ADP (adp_ai):
Output a pick number (1-200) where LOWER numbers = earlier picks = MORE valuable.
This is the OPPOSITE of bid ceiling — do NOT conflate the two. A high bid ceiling
means a LOW adp_ai.

  Pick 1   = first overall (most valuable)
  Pick 200 = last pick (least valuable)

  A player you'd bid $70 in auction is typically a pick 1-12 in snake.
  A player you'd bid $5  in auction is typically a pick 100+ in snake.

Tier framework (12-team snake):
  Tier 1 (elite):       picks 1-12
  Tier 2 (strong):      picks 13-36
  Tier 3 (solid):       picks 37-72
  Tier 4 (depth):       picks 73-120
  Tier 5 (late/flier):  picks 121-180

Adjust WITHIN the tier:
  - availability_risk="concern"  → push 10-15 picks LATER (higher number)
  - value_gap strongly positive  → push earlier (lower number)
  - QB / K / DEF → go MUCH later than their auction dollars imply. This is the
    biggest auction-vs-snake difference: a $38 auction QB (e.g. Lamar Jackson)
    is typically pick ~35-40 in snake PPR, not a first-round pick. Kickers and
    defenses are picks 130+ regardless of value.

QB ADP guidance — differentiate by QB tier, and NEVER cluster QBs in the same
pick range:
  Elite (Lamar Jackson, Josh Allen, Jalen Hurts — injury-permitting):
    → picks 25-40 (late 3rd / early 4th)
  Strong (Burrow, Daniels, Murray, Mahomes — note availability risk):
    → picks 45-80 (rounds 4-7)
  Standard starter QBs:
    → picks 85-130 (rounds 8-11)
  Backup / streaming QBs:
    → picks 130+ (round 11+)
Spread QBs across rounds using this framework — do not give several QBs the same
adp_ai. QB is the deepest position: 32 starters, only 12 needed. Wait on QB.

adp_ai is MANDATORY — output it for EVERY player, never null, never omitted.
If you are uncertain, default to the tier midpoint:
  Tier 1 → 6, Tier 2 → 24, Tier 3 → 54, Tier 4 → 96, Tier 5 → 150

(snake_flag is NOT requested here — it is computed deterministically from
adp_diff, which depends on your adp_ai and therefore can't be known at inference.)

Output ONLY a JSON array. No commentary outside the JSON."""


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class ValuationAgent(BaseAgent):
    AGENT_NAME = "valuation_agent"
    AGENT_MODEL = SONNET
    AGENT_MAX_TOKENS = 600

    async def run_all(self) -> dict:
        """Run valuation agent for all valued players, batched by tier."""
        # 1. Load all valued players in one query
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Player)
                .where(Player.recommended_bid_ceiling.isnot(None))
                .options(
                    selectinload(Player.profile),
                    selectinload(Player.injury_profile),
                    selectinload(Player.schedule),
                    selectinload(Player.dependencies),
                    selectinload(Player.historic_prices),
                )
                .order_by(Player.tier.asc().nulls_last(), Player.recommended_bid_ceiling.desc())
            )
            players = result.scalars().all()

        if not players:
            logger.warning("No valued players found — skipping valuation agent")
            return {"processed": 0, "skipped": 0}

        # 2. Build context dicts and group by tier
        tier_groups: dict[int, list[tuple[Player, dict]]] = {}
        for p in players:
            ctx = self._build_player_context(p)
            tier = p.tier or 0
            if tier not in tier_groups:
                tier_groups[tier] = []
            tier_groups[tier].append((p, ctx))

        # 3. Process each tier group with appropriate batching
        processed = 0
        skipped = 0
        results_map: dict[str, dict] = {}  # player_name -> parsed result

        for tier, group in sorted(tier_groups.items()):
            if tier == 1:
                # Individual Sonnet calls for tier 1
                for player, ctx in group:
                    result = await self._process_batch(
                        [ctx],
                        entity_id=player.name,
                        # 800 (was 600): tier-1 players have the longest
                        # auction_notes; headroom so the JSON (now with adp_ai)
                        # never truncates before the last field.
                        model=SONNET,
                        max_tokens=800,
                    )
                    if result:
                        for r in result:
                            results_map[r["player_name"]] = r
                        processed += 1
                    else:
                        skipped += 1

            elif tier in (2, 3):
                # Batches of 5 for tiers 2-3 (Sonnet)
                batch_size = 5
                for i in range(0, len(group), batch_size):
                    batch = group[i:i + batch_size]
                    contexts = [ctx for _, ctx in batch]
                    result = await self._process_batch(
                        contexts,
                        entity_id=f"tier{tier}_batch_{i // batch_size + 1}",
                        model=SONNET,
                        max_tokens=600 * len(contexts),
                    )
                    if result:
                        for r in result:
                            results_map[r["player_name"]] = r
                        processed += len(batch)
                    else:
                        skipped += len(batch)

            else:
                # Batches of 10 for tiers 4-5 (Haiku)
                batch_size = 10
                for i in range(0, len(group), batch_size):
                    batch = group[i:i + batch_size]
                    contexts = [ctx for _, ctx in batch]
                    result = await self._process_batch(
                        contexts,
                        entity_id=f"tier{tier}_batch_{i // batch_size + 1}",
                        model=HAIKU,
                        max_tokens=400 * len(contexts),
                    )
                    if result:
                        for r in result:
                            results_map[r["player_name"]] = r
                        processed += len(batch)
                    else:
                        skipped += len(batch)

        # 4. Write results to DB
        await self._write_results(players, results_map)

        # 5. Global re-rank by adp_ai (clean 1-N) across ALL players with an ADP.
        await self._compute_adp_ranks()

        logger.info(
            "Valuation agent complete: %d processed, %d skipped",
            processed, skipped,
        )
        return {"processed": processed, "skipped": skipped}

    async def _compute_adp_ranks(self) -> None:
        """Assign adp_rank (clean 1-N by adp_ai), then derive adp_diff + snake_flag.

        Runs after all players are written — it's a global ranking, so a player
        only gets a rank if they have an adp_ai. adp_diff = fp_rank - adp_rank
        (both clean ranks) and snake_flag is derived from that diff, so both must
        be computed here, after adp_rank exists — not in _write_results.
        """
        async with AsyncSessionLocal() as session:
            players = (
                await session.execute(
                    select(Player)
                    .where(Player.adp_ai.isnot(None))
                    .order_by(Player.adp_ai.asc())
                    .options(selectinload(Player.profile))
                )
            ).scalars().all()
            count = assign_adp_ranks(players)

            for p in players:
                p.adp_diff = compute_adp_diff(p.adp_fantasypros, p.adp_rank)
                projected_ppr = None
                if p.profile and p.profile.clean_season_baseline:
                    projected_ppr = p.profile.clean_season_baseline.get("ppr_points")
                # adp_diff keeps the raw rank gap (for display); the flag is
                # neutralized past the draftable window via adp_rank.
                p.snake_flag = classify_snake_flag(
                    p.adp_diff, projected_ppr, p.position, p.adp_rank
                )

            await session.commit()
        logger.info("adp_rank + adp_diff + snake_flag computed for %d players", count)

    def _build_player_context(self, p: Player) -> dict:
        """Build context dict for a single player from pre-loaded ORM data."""
        ctx: dict = {
            "player_name": p.name,
            "position": p.position,
            "team": p.team_abbr,
            "age": p.age,
            "tier": p.tier,
            "is_rookie": p.is_rookie or False,
        }

        # Math-derived values
        ctx["math_bid_ceiling"] = float(p.recommended_bid_ceiling) if p.recommended_bid_ceiling else None
        ctx["system_value"] = float(p.baseline_value) if p.baseline_value else None
        ctx["market_value"] = float(p.market_value) if p.market_value else None
        ctx["value_gap"] = float(p.value_gap) if p.value_gap else None
        ctx["value_gap_signal"] = p.value_gap_signal
        ctx["ceiling_value"] = float(p.ceiling_value) if p.ceiling_value else None
        ctx["floor_value"] = float(p.floor_value) if p.floor_value else None

        # Consensus ADP (shared across all users — no league-specific data)
        if p.market_value_fantasypros is not None:
            ctx["market_value_fantasypros"] = float(p.market_value_fantasypros)

        # Prior season actual auction price (from historic archive)
        prior_year = get_current_season() - 1
        for hp in (p.historic_prices or []):
            if hp.season_year == prior_year:
                ctx["prior_season_price"] = float(hp.price)
                break

        # Profile data
        if p.profile:
            prof = p.profile
            baseline = prof.clean_season_baseline or {}
            ctx["projected_ppr"] = baseline.get("ppr_points")
            ctx["upside_ppr"] = baseline.get("upside_ppr")
            ctx["downside_ppr"] = baseline.get("downside_ppr")
            ctx["projection_confidence"] = prof.confidence
            ctx["projection_reasoning"] = prof.projection_reasoning
            ctx["career_trajectory"] = prof.career_trajectory
            ctx["role"] = prof.role_classification
            ctx["profile_source"] = prof.profile_source
            ctx["breakout_flag"] = prof.breakout_flag or False
            ctx["positional_scarcity"] = prof.positional_scarcity_tier
        else:
            ctx["projected_ppr"] = None

        # Injury risk
        if p.injury_profile:
            ip = p.injury_profile
            ctx["injury_risk"] = ip.overall_risk_level
            # Games-based availability (durable/monitor/concern) — drives the
            # snake-ADP "concern → push later" adjustment.
            ctx["availability_risk"] = ip.availability_risk
            ctx["injury_modifier"] = float(ip.risk_adjusted_value_modifier) if ip.risk_adjusted_value_modifier else None
            active_flags = ip.pattern_flags or []
            if ip.workload_cliff_flag:
                active_flags.append("WORKLOAD_CLIFF")
            if ip.high_mileage_flag:
                active_flags.append("HIGH_MILEAGE")
            if ip.post_acl_flag:
                active_flags.append("POST_ACL")
            ctx["injury_flags"] = active_flags
        else:
            ctx["injury_risk"] = None

        # Schedule
        if p.schedule:
            ctx["schedule_grade"] = p.schedule.full_season_grade
            ctx["playoff_grade"] = p.schedule.playoff_window_grade
            ctx["schedule_score"] = float(p.schedule.schedule_score) if p.schedule.schedule_score else None
        else:
            ctx["schedule_grade"] = None

        # Dependency flags
        dep_flags = []
        for dep in (p.dependencies or []):
            dep_flags.append({
                "flag_type": dep.flag_type,
                "trigger": dep.trigger_player_name,
                "impact_pct": float(dep.value_impact_pct) if dep.value_impact_pct else None,
            })
        if dep_flags:
            ctx["dependency_flags"] = dep_flags

        return ctx

    async def _process_batch(
        self,
        contexts: list[dict],
        entity_id: str,
        model: str,
        max_tokens: int,
    ) -> list[dict] | None:
        """Process a batch of players through the AI model."""
        user_content = json.dumps(contexts, default=str)

        try:
            raw = await self.call_once(
                system=SYSTEM_PROMPT,
                user=user_content,
                # version is part of the cache key, so a prompt change (version
                # bump) invalidates cached results without a manual clear.
                input_data={
                    "players": [c["player_name"] for c in contexts],
                    "version": VALUATION_AGENT_VERSION,
                },
                entity_id=entity_id,
                model=model,
                max_tokens=max_tokens,
            )
        except Exception:
            logger.exception("Valuation agent call failed for %s", entity_id)
            return None

        if not raw:
            return None

        try:
            parsed = parse_json_output(raw)
            if isinstance(parsed, dict):
                parsed = [parsed]
            return parsed
        except Exception:
            logger.exception("Failed to parse valuation output for %s", entity_id)
            return None

    async def _write_results(
        self,
        players: list[Player],
        results_map: dict[str, dict],
    ) -> None:
        """Write AI valuation results back to the players table."""
        # Build name -> player mapping
        name_to_player: dict[str, Player] = {}
        for p in players:
            name_to_player[p.name] = p

        max_bids = {"RB": 80, "WR": 70, "QB": 50, "TE": 45}
        written = 0

        async with AsyncSessionLocal() as session:
            for player_name, result in results_map.items():
                player = name_to_player.get(player_name)
                if not player:
                    logger.warning("Result for unknown player: %s", player_name)
                    continue

                # Fetch the managed instance
                db_player = await session.get(Player, player.id)
                if not db_player:
                    continue

                # Clamp ai_bid_ceiling to position max
                pos_max = max_bids.get(db_player.position, 80)
                ai_ceiling = result.get("ai_bid_ceiling")
                if ai_ceiling is not None:
                    ai_ceiling = max(1, min(int(ai_ceiling), pos_max))

                db_player.ai_bid_ceiling = ai_ceiling

                # Snake-draft ADP — clamp to the position's pick range (LOWER =
                # earlier). Mirrors the bid-ceiling clamp but inverted. Stamp
                # adp_scoring the same way sync_adp does, so the column is
                # consistent regardless of which step populated the ADP.
                adp_ai = clamp_adp(result.get("adp_ai"), db_player.position)
                if adp_ai is not None:
                    db_player.adp_ai = adp_ai
                db_player.adp_scoring = VALUATION_SCORING

                # adp_diff + snake_flag are computed in _compute_adp_ranks(),
                # AFTER adp_rank is assigned — the diff is fp_rank - adp_rank, and
                # adp_rank is a global ordering that isn't known until every
                # player has been written.

                db_player.ai_confidence_floor = result.get("confidence_floor")
                db_player.ai_confidence_ceiling = result.get("confidence_ceiling")
                db_player.value_assessment = result.get("value_assessment")
                note = result.get("auction_note")
                if note:
                    # Sanitize: strip league-specific language the model
                    # sometimes generates despite prompt instructions
                    note = re.sub(
                        r"(?i)\b(your|the|this) league (paid|spent|valued|priced)\b",
                        "consensus ADP was",
                        note,
                    )
                    note = re.sub(r"(?i)\bin your league\b", "at consensus", note)
                db_player.auction_note = note
                db_player.pay_up_flag = result.get("pay_up_flag", False)
                db_player.nomination_target_flag = result.get("nomination_target_flag", False)
                written += 1

            await session.commit()

        logger.info("Wrote AI valuations for %d players", written)
