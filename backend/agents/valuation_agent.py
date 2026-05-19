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
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an auction draft strategist for a 12-team PPR fantasy football league.
Each team has a $200 budget. The target is $185 on 7 starting skill players (QB, 2×RB, 2×WR, TE, FLEX).

You receive players with their MATH-DERIVED bid ceilings (from a PAR valuation engine) plus
full scouting context. Your job is to apply STRATEGIC JUDGMENT to calibrate the final bid ceiling.

For each player, output a JSON object with these fields:
{
  "player_name": "string — must match input exactly",
  "ai_bid_ceiling": integer — your recommended max bid ($1 minimum),
  "confidence_floor": integer — lowest you'd bid in a cautious room,
  "confidence_ceiling": integer — highest you'd go in an aggressive room,
  "value_assessment": "string — one of: elite_value, good_value, fair_value, slight_overpay, avoid",
  "auction_note": "string — 1-2 sentences of tactical advice for draft day",
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
- auction_note should reference the player's specific situation, not generic advice
- Reference the prior season's consensus ADP to assess current value
- NEVER say "your league paid" or "in your league" — this analysis is shared across all users
- Say "consensus ADP was $X" or "the market typically prices this player at $X"
- Value assessment considers: projection confidence, injury risk, schedule, dependency flags, positional scarcity
- Max realistic bids: RB=$80, WR=$70, QB=$50, TE=$45. Never exceed these.

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
                        model=SONNET,
                        max_tokens=600,
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

        logger.info(
            "Valuation agent complete: %d processed, %d skipped",
            processed, skipped,
        )
        return {"processed": processed, "skipped": skipped}

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
                input_data={"players": [c["player_name"] for c in contexts]},
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
