"""
Live Draft Engine — real-time recommendation orchestrator.

Model: claude-sonnet-4-6 (real-time decision-making)
Max tokens: 400 per recommendation
Target: under 2000ms end-to-end per nomination

This is the ONE engine that calls messages.create() directly
(per PATTERNS.md Pattern 6). Every nomination has unique context,
so BaseAgent caching would be counterproductive.

Architecture:
  nomination event → _get_player_record (DB query)
                   → DependencyResolver (pure Python)
                   → budget constraints (pure Python)
                   → OpponentThreatAnalyzer (pure Python)
                   → single Sonnet call (400 tokens)
                   → WebSocket broadcast to React UI
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.agents.base_agent import SONNET, get_client, parse_json_output
from backend.engines.draft_state_manager import DraftPick, DraftStateManager
from backend.engines.dependency_resolver import DependencyResolver
from backend.engines.opponent_threat import OpponentThreatAnalyzer
from backend.engines.valuation import MAX_REALISTIC_BID
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile
from backend.models.dependency import PlayerDependency

logger = logging.getLogger(__name__)

_MAX_TOKENS = 400

_SYSTEM_PROMPT = """You are a fantasy football auction draft advisor.
Given pre-computed analytics about a nominated player, output a single JSON recommendation.

Output ONLY a valid JSON object. No explanation, no preamble, no markdown fences.
Your entire response must be parseable by json.loads().

Output schema:
{
  "action": "buy|bid_to|block|pass",
  "bid_ceiling": integer,
  "reasoning": "one sentence max",
  "confidence": "high|medium|low"
}

Rules:
- "buy" = aggressively pursue up to bid_ceiling
- "bid_to" = monitor but drop at bid_ceiling
- "block" = bid to prevent opponent combo, even above personal value
- "pass" = do not bid
- bid_ceiling must never exceed spendable_budget
- If active_flags include "displaced", factor the value reduction into ceiling
- If block_value > personal_value AND budget allows, recommend "block"
- manager_styles shows opponent draft tendencies (hero_rb = will overpay for RBs, zero_rb = avoids RBs)
- If an aggressive/hero_rb opponent is likely bidding, set ceiling higher to compete
"""


_SNAKE_SYSTEM_PROMPT = """You are an expert fantasy football snake draft advisor.

Your job: given a player available NOW and the user's current roster, recommend
whether to DRAFT this player or WAIT.

CRITICAL DIFFERENCES FROM AUCTION:
- No bidding — you either draft this player with your current pick or pass.
- If you pass, this player may be gone by your next pick.
- Opportunity cost matters — using an early pick on a QB means missing elite RB/WR value.
- Roster construction matters — don't stack positions you've already filled.

DRAFT or WAIT decision framework:
DRAFT when:
  - The player's adp_ai is at or near the current pick number (good value).
  - The player fills a roster need.
  - The player is a tier above what will be available at your next pick.
WAIT when:
  - The player's adp_ai is much later than the current pick (a reach).
  - You already have your starters at that position.
  - Better value exists at a position of greater need.

POSITION DRAFT ORDER (PPR):
  Round 1-3:   Elite RB/WR only
  Round 4-6:   RB/WR/TE (Kelce tier)
  Round 7-9:   Best available RB/WR/TE
  Round 10-12: QB (unless an elite one is available)
  Round 13-15: K and DEF last

QB note: In PPR snake, QBs go rounds 8-12. Never use a top-5 pick on a QB unless
Lamar Jackson tier — and even then consider waiting; QB is the deepest position.

FORBIDDEN:
- Never mention specific injury diagnoses.
- Never reference body parts.
- No "chronic condition" language.

OUTPUT FORMAT (JSON only, no markdown fences):
{
  "action": "draft" | "wait",
  "reasoning": "1-2 sentences max",
  "adp_ai": <number or null>,
  "adp_fp": <number or null>,
  "adp_diff": <adp_fp - adp_ai, positive = we like them more than consensus>,
  "position_need": "high" | "medium" | "low",
  "confidence": "high" | "medium" | "low",
  "tier": <tier number or null>
}
"""


class LiveDraftEngine:
    """
    Main orchestrator for live draft recommendations.

    Processes nomination events through the full analysis pipeline
    and emits recommendations via WebSocket within 2 seconds.
    """

    def __init__(
        self,
        state: DraftStateManager,
        resolver: DependencyResolver,
        threat_analyzer: OpponentThreatAnalyzer,
        db_session_factory,
        ws_manager,
    ):
        self.state = state
        self.resolver = resolver
        self.threat_analyzer = threat_analyzer
        self._db_session_factory = db_session_factory
        self.ws_manager = ws_manager
        self._client = get_client()
        self.last_recommendation: dict | None = None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def handle_event(self, event: dict) -> None:
        """Route a bridge event to the appropriate handler."""
        event_type = event.get("type")
        if event_type == "nomination":
            await self.on_nomination(event)
        elif event_type == "draft_pick":
            await self.on_pick_confirmed(event)
        elif event_type == "bid_update":
            await self.on_bid_update(event)

    async def on_nomination(self, event: dict) -> None:
        """Route a nomination to the snake or auction recommendation path."""
        if self.state.is_snake:
            await self._on_nomination_snake(event)
        else:
            await self._on_nomination_auction(event)

    async def _on_nomination_auction(self, event: dict) -> None:
        """
        Process an AUCTION nomination event and emit a recommendation.
        Must complete in under 2000ms.
        """
        start = time.monotonic()
        player_id = event.get("player_id", "")

        # Step 1: Pull player record (single DB query)
        record = await self._get_player_record(player_id)
        if not record:
            await self._emit_unknown_player(player_id, event)
            return

        # Step 2: Apply dependency flags (pure Python)
        drafted_ids = self.state.get_drafted_player_ids()
        active_flags, flag_modifier = self.resolver.apply_active_flags(
            record.get("dependencies", []), drafted_ids
        )

        # Step 3: Calculate budget constraints (pure Python)
        spendable = self.state.get_spendable_on_this_player()

        # Step 4: Calculate live bid ceiling
        live_ceiling = self._calculate_live_bid_ceiling(
            record, flag_modifier, spendable
        )

        # Step 5: Calculate block values per opponent (pure Python)
        block_analysis: dict[str, float] = {}
        for team_id, roster in self.state.opponent_rosters.items():
            budget = self.state.opponent_budgets.get(team_id, 0)
            block_val = self.threat_analyzer.get_block_value(
                record, roster, budget
            )
            if block_val > 0:
                block_analysis[team_id] = block_val
        max_block_value = max(block_analysis.values(), default=0.0)

        # Step 6: Get opponent combo alerts and manager styles
        opponent_alerts: list[str] = []
        manager_styles: dict[str, str] = {}
        for team_id, roster in self.state.opponent_rosters.items():
            combos = self.threat_analyzer.get_active_combo_flags(roster)
            opponent_alerts.extend(combos)
            # Collect manager style for opponents who could compete for this player
            tendency = self.threat_analyzer.tendencies.get(team_id, {})
            if tendency.get("style"):
                manager_styles[team_id] = tendency["style"]

        # Step 7: Single Sonnet call
        context = {
            "player_name": record["name"],
            "position": record["position"],
            "team": record.get("team_abbr", ""),
            "tier": record.get("tier"),
            "system_value": record.get("system_value", 0),
            "market_value": record.get("market_value", 0),
            "pre_computed_ceiling": live_ceiling,
            "active_flags": [
                {
                    "flag_type": f.get("flag_type"),
                    "trigger": f.get("trigger_player_name"),
                    "reason": f.get("reason"),
                    "impact_pct": f.get("value_impact_pct"),
                }
                for f in active_flags
            ],
            "flag_modifier": flag_modifier,
            "spendable_budget": spendable,
            "max_block_value": max_block_value,
            "budget_allows_block": spendable >= max_block_value > 0,
            "opponent_alerts": opponent_alerts,
            "notes": record.get("notes", ""),
            "pay_up_flag": record.get("pay_up_flag", False),
            "value_assessment": record.get("value_assessment", ""),
            "manager_styles": manager_styles,
        }

        recommendation = await self._get_recommendation(context, record)

        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "Recommendation for %s in %.0fms: %s $%s",
            record["name"], elapsed,
            recommendation.get("action"),
            recommendation.get("bid_ceiling"),
        )

        # Step 8: Broadcast to React UI
        message = {
            "type": "recommendation",
            **recommendation,
            "elapsed_ms": round(elapsed),
        }
        self.last_recommendation = message
        await self.ws_manager.broadcast(message)

    async def on_pick_confirmed(self, event: dict) -> None:
        """Update state after every confirmed pick."""
        pick = DraftPick(
            player_id=event.get("player_id", ""),
            team_id=event.get("team_id", ""),
            price=event.get("final_price", 0),
            player_name=event.get("player_name", ""),
            position=event.get("position", ""),
        )
        self.state.record_pick(pick)

        # Recalculate opponent threat scores and check for new combos
        for team_id, roster in self.state.opponent_rosters.items():
            combos = self.threat_analyzer.get_active_combo_flags(roster)
            if combos:
                score = self.threat_analyzer.get_threat_score(roster, team_id=team_id)
                await self.ws_manager.broadcast({
                    "type": "opponent_combo_alert",
                    "team_id": team_id,
                    "combos": combos,
                    "threat_score": score,
                })

    async def on_bid_update(self, event: dict) -> None:
        """Forward bid updates to UI (state tracking only)."""
        await self.ws_manager.broadcast({
            "type": "bid_update",
            "player_id": event.get("player_id"),
            "current_bid": event.get("current_bid"),
            "current_bidder": event.get("current_bidder"),
        })

    # ------------------------------------------------------------------
    # Player record loading
    # ------------------------------------------------------------------

    async def _get_player_record(self, yahoo_player_id: str) -> dict | None:
        """
        Single DB query with eager loads for Player + profile + injury + dependencies.
        Converts ORM objects to a plain dict for pure Python processing.
        """
        async with self._db_session_factory() as session:
            stmt = (
                select(Player)
                .options(
                    selectinload(Player.profile),
                    selectinload(Player.injury_profile),
                    selectinload(Player.dependencies).selectinload(
                        PlayerDependency.trigger_player
                    ),
                )
                .where(Player.yahoo_player_id == yahoo_player_id)
            )
            result = await session.execute(stmt)
            player = result.scalar_one_or_none()

        if not player:
            return None

        risk_level = "low"
        availability_risk = None
        if player.injury_profile:
            if player.injury_profile.overall_risk_level:
                risk_level = player.injury_profile.overall_risk_level
            availability_risk = player.injury_profile.availability_risk

        projected_ppr = None
        if player.profile and player.profile.clean_season_baseline:
            projected_ppr = player.profile.clean_season_baseline.get("ppr_points")

        return {
            "yahoo_player_id": player.yahoo_player_id,
            "player_id": str(player.id),
            "name": player.name,
            "position": player.position or "",
            "team_abbr": player.team_abbr or "",
            "tier": player.tier,
            "system_value": float(player.baseline_value or 0),
            "market_value": float(player.market_value or 0),
            "ai_bid_ceiling": player.ai_bid_ceiling,
            "recommended_bid_ceiling": float(player.recommended_bid_ceiling or 0),
            # ADP (snake path) — null until a pipeline run populates them.
            "adp_ai": float(player.adp_ai) if player.adp_ai is not None else None,
            "adp_fantasypros": (
                float(player.adp_fantasypros)
                if player.adp_fantasypros is not None else None
            ),
            "adp_scoring": player.adp_scoring,
            "availability_risk": availability_risk,
            "projected_ppr": projected_ppr,
            "risk_level": risk_level,
            "notes": player.notes or "",
            "pay_up_flag": bool(player.pay_up_flag),
            "value_assessment": player.value_assessment or "",
            "dependencies": [
                {
                    "flag_type": dep.flag_type,
                    "trigger_yahoo_player_id": (
                        dep.trigger_player.yahoo_player_id
                        if dep.trigger_player else None
                    ),
                    "trigger_player_name": dep.trigger_player_name,
                    "trigger_condition": dep.trigger_condition,
                    "value_impact_pct": float(dep.value_impact_pct or 0),
                    "confidence": dep.confidence,
                }
                for dep in (player.dependencies or [])
            ],
        }

    # ------------------------------------------------------------------
    # Bid ceiling calculation
    # ------------------------------------------------------------------

    def _calculate_live_bid_ceiling(
        self,
        record: dict,
        flag_modifier: float,
        spendable: int,
    ) -> int:
        """
        Calculate the live bid ceiling for a nominated player.

        Starts from the pre-computed ceiling (ai_bid_ceiling or recommended_bid_ceiling),
        applies the dependency flag modifier, and constrains by budget and position cap.
        """
        # Start from best available pre-computed ceiling
        base_ceiling = float(
            record.get("ai_bid_ceiling")
            or record.get("recommended_bid_ceiling")
            or record.get("system_value")
            or 1
        )

        # Apply dependency flag modifier
        if flag_modifier != 0:
            adjusted = base_ceiling * (1.0 + flag_modifier)
        else:
            adjusted = base_ceiling

        # Position cap
        pos = record.get("position", "")
        max_bid = MAX_REALISTIC_BID.get(pos, 80)

        # Budget constraint
        ceiling = min(adjusted, spendable, max_bid)
        return max(1, int(round(ceiling)))

    # ------------------------------------------------------------------
    # AI recommendation
    # ------------------------------------------------------------------

    async def _get_recommendation(
        self, context: dict, record: dict
    ) -> dict[str, Any]:
        """
        Single Sonnet call. 400 tokens max. JSON-only output.
        Merges AI output with pre-computed context.
        """
        response = await self._client.messages.create(
            model=SONNET,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": json.dumps(context, default=str),
            }],
        )

        raw_text = response.content[0].text
        try:
            ai_output = parse_json_output(raw_text)
        except Exception:
            logger.warning("Failed to parse recommendation JSON: %s", raw_text[:200])
            ai_output = {
                "action": "pass",
                "bid_ceiling": context.get("pre_computed_ceiling", 1),
                "reasoning": "AI response parse error — defaulting to pass",
                "confidence": "low",
            }

        # Merge AI output with pre-computed context
        return {
            **ai_output,
            "player_name": record["name"],
            "position": record.get("position", ""),
            "system_value": record.get("system_value", 0),
            "market_value": record.get("market_value", 0),
            "pre_computed_ceiling": context["pre_computed_ceiling"],
            "active_flags": context["active_flags"],
            "opponent_alerts": context["opponent_alerts"],
            "block_value": context["max_block_value"],
            "budget_allows_block": context["budget_allows_block"],
            "budget_summary": {
                "your_remaining": self.state.get_your_remaining_budget(),
                "spendable_on_this_player": context["spendable_budget"],
                "minimum_completion_budget": self.state.get_minimum_completion_budget(),
                "roster_slots_remaining": self.state.get_roster_slots_remaining(),
            },
        }

    async def _emit_unknown_player(
        self, player_id: str, event: dict
    ) -> None:
        """Emit a pass recommendation for players not in our DB."""
        message = {
            "type": "recommendation",
            "action": "pass",
            "bid_ceiling": 1,
            "reasoning": f"Player {player_id} not in draft bible — manual evaluation needed",
            "confidence": "low",
            "player_name": event.get("player_name", player_id),
            "position": "",
            "system_value": 0,
            "market_value": 0,
            "pre_computed_ceiling": 1,
            "active_flags": [],
            "opponent_alerts": [],
            "block_value": 0,
            "budget_allows_block": False,
            "budget_summary": {
                "your_remaining": self.state.get_your_remaining_budget(),
                "spendable_on_this_player": self.state.get_spendable_on_this_player(),
                "minimum_completion_budget": self.state.get_minimum_completion_budget(),
                "roster_slots_remaining": self.state.get_roster_slots_remaining(),
            },
        }
        self.last_recommendation = message
        await self.ws_manager.broadcast(message)

    # ------------------------------------------------------------------
    # Snake draft path
    # ------------------------------------------------------------------

    async def _on_nomination_snake(self, event: dict) -> None:
        """Snake draft recommendation via Sonnet.

        Unlike auction, there's no bid ceiling or price negotiation — the
        question is "should I spend my current pick on this player, given my
        roster and what's left?" The adp_ai number (AI-generated by
        valuation_agent) anchors the value; Sonnet adds the DRAFT/WAIT call.
        """
        start = time.monotonic()
        player_id = event.get("player_id", "")

        record = await self._get_player_record(player_id)
        if not record:
            await self._emit_unknown_player(player_id, event)
            return

        context = {
            "player_name": record["name"],
            "position": record.get("position"),
            "team": record.get("team_abbr"),
            "tier": record.get("tier"),
            "adp_ai": record.get("adp_ai"),
            "adp_fantasypros": record.get("adp_fantasypros"),
            "current_pick": event.get("current_pick"),
            "scoring_format": self.state.scoring_format,
            "my_roster": self.state.get_roster_summary(),
            "availability_risk": record.get("availability_risk"),
            "projected_ppr": record.get("projected_ppr"),
        }

        prompt = self._build_snake_prompt(context)
        response = await self._client.messages.create(
            model=SONNET,
            max_tokens=600,
            system=_SNAKE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        rec = self._parse_snake_recommendation(
            response.content[0].text, record, context
        )
        elapsed = (time.monotonic() - start) * 1000
        rec["elapsed_ms"] = round(elapsed)
        self.last_recommendation = rec
        logger.info(
            "Snake rec for %s in %.0fms: %s",
            record["name"], elapsed, rec.get("action"),
        )
        await self.ws_manager.broadcast(rec)

    def _build_snake_prompt(self, context: dict) -> str:
        roster = context["my_roster"]
        positions_filled = {
            pos: len(players) for pos, players in roster.items() if players
        }

        def _show(v):
            return v if v is not None else "N/A"

        return (
            f"Player available NOW: {context['player_name']}\n"
            f"Position: {context['position']}\n"
            f"Team: {context['team']}\n"
            f"Tier: {_show(context['tier'])}\n\n"
            f"ADP data:\n"
            f"  AI ADP:  {_show(context['adp_ai'])}\n"
            f"  FP ADP:  {_show(context['adp_fantasypros'])}\n"
            f"  Current pick: {context['current_pick'] if context['current_pick'] is not None else 'unknown'}\n"
            f"  Scoring: {context['scoring_format']}\n\n"
            f"Availability: {context['availability_risk'] or 'unknown'}\n"
            f"Projected PPR: {_show(context['projected_ppr'])}\n\n"
            f"My roster so far:\n{self._format_roster(positions_filled)}\n\n"
            f"Should I draft {context['player_name']} with my current pick?"
        )

    def _format_roster(self, positions_filled: dict) -> str:
        if not positions_filled:
            return "  (empty — no picks yet)"
        order = ["QB", "RB", "WR", "TE", "K", "DEF"]
        lines = [f"  {p}: {positions_filled[p]}" for p in order if p in positions_filled]
        lines += [
            f"  {p}: {n}" for p, n in positions_filled.items() if p not in order
        ]
        return "\n".join(lines)

    def _parse_snake_recommendation(
        self, text: str, player: dict, context: dict
    ) -> dict:
        try:
            data = parse_json_output(text)
            if not isinstance(data, dict):
                raise ValueError("snake recommendation was not a JSON object")
        except Exception:
            logger.warning("Failed to parse snake recommendation JSON: %s", text[:200])
            data = {"action": "wait", "reasoning": text[:200], "confidence": "low"}

        adp_ai = data.get("adp_ai")
        if adp_ai is None:
            adp_ai = context.get("adp_ai")
        adp_fp = data.get("adp_fp")
        if adp_fp is None:
            adp_fp = context.get("adp_fantasypros")
        adp_diff = data.get("adp_diff")
        if adp_diff is None and adp_ai is not None and adp_fp is not None:
            adp_diff = round(float(adp_fp) - float(adp_ai), 1)

        return {
            "type": "recommendation",
            "action": data.get("action", "wait"),
            "reasoning": data.get("reasoning", ""),
            "player_name": context["player_name"],
            "position": context["position"],
            "adp_ai": adp_ai,
            "adp_fp": adp_fp,
            "adp_diff": adp_diff,
            "position_need": data.get("position_need", "medium"),
            "confidence": data.get("confidence", "medium"),
            "tier": data.get("tier") if data.get("tier") is not None else player.get("tier"),
            "elapsed_ms": 0,
        }
