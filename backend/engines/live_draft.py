"""
Live Draft Engine — real-time recommendation orchestrator.

DETERMINISTIC-FIRST: THE ENGINE DECIDES, SONNET EXPLAINS.
The engine computes the pick / bid (roster needs, budget feasibility, round-phase
rules, flags, blocks — all pure Python) and broadcasts it IMMEDIATELY. Sonnet is
then called ASYNC purely to enrich the already-displayed recommendation with
trend/situation nuance — it never changes the player, never changes the bid
number, never does arithmetic. A model failure means the nuance text doesn't
arrive; the recommendation is already on screen.

Model: claude-sonnet-4-6 (nuance/explanation only)
This is the ONE engine that calls messages.create() directly
(per PATTERNS.md Pattern 6). Every nomination has unique context and the
prompts are far below Sonnet's 2,048-token cache minimum, so neither BaseAgent
caching nor prompt caching applies here.

Architecture:
  nomination/your_turn event → DB query + pure-Python decision
                             → BROADCAST deterministic recommendation (instant)
                             → async Sonnet explain call (retried, usage-logged)
                             → BROADCAST enriched reasoning (same pick/number)
"""
from __future__ import annotations

import asyncio
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
from backend.models.player_format_values import PlayerFormatValues

logger = logging.getLogger(__name__)

_MAX_TOKENS = 400

# Draft-day burst resilience: the shared client has max_retries=0 (BaseAgent does
# its own backoff); the live-draft path has no backoff of its own, so give ITS
# client SDK retries. Scoped via with_options — the shared client is untouched.
_DRAFT_CLIENT_RETRIES = 2

# ---------------------------------------------------------------------------
# Round-phase guardrails — HARD RULES IN CODE, not prompt guidance. (Sonnet
# violated its own prompt's QB-round framework twice in recon; constraints that
# can be computed live here.) Expressed as rounds-from-the-END for K/DEF so any
# roster size works, and a fixed earliest round for QB.
# ---------------------------------------------------------------------------
KDEF_FINAL_ROUNDS = 3     # K/DEF only in the last 3 rounds (unless nothing else fills a need)
QB_MIN_ROUND = 7          # never recommend a QB before round 7
# Phase 2 (non-PPR only): if the FORMAT-MATCHED market ADP falls this many picks past the
# current pick, the player will clearly still be available → advise WAIT even at a need.
# A reception-dependent player drafts much later in Half/Standard, so this flips a PPR
# "take now" to a Standard "wait". Gated on non-PPR so PPR stays byte-identical.
SNAKE_FORMAT_WAIT_MARGIN = 12
# Need-vs-BPA rank window (the "don't reach absurdly far" guard). A BPA that
# still adds STARTER value is itself a need pick and always wins; when the BPA
# is bench-only, the best need-position player wins UNLESS he trails the BPA by
# more than this many ADP ranks. Tuned against the recon scenarios: the last
# startable TE trailed the bench-only BPA by 40 ranks and must still win (S5),
# so the window sits well above that while still refusing triple-digit reaches.
NEED_RANK_WINDOW = 75

_SYSTEM_PROMPT = """You are a fantasy football auction draft analyst.

THE RECOMMENDATION ENGINE HAS ALREADY DECIDED — the action and bid ceiling are
final and were computed deterministically (value, dependency flags, budget
feasibility, roster needs, block values). Your ONLY job is to EXPLAIN the
decision and add nuance: player trend, situation, opponent context. You never
change the action, never change the number, never do arithmetic.

Output ONLY a valid JSON object. No preamble, no markdown fences.

Output schema:
{
  "reasoning": "1-2 sentences explaining the engine's decision, with any trend/situation nuance",
  "confidence": "high|medium|low"
}

FORBIDDEN: never mention specific injury diagnoses, body parts, or "chronic"
language.
"""


_SNAKE_SYSTEM_PROMPT = """You are an expert fantasy football snake draft analyst.

THE RECOMMENDATION ENGINE HAS ALREADY DECIDED whether to DRAFT or WAIT on this
player — the decision was computed deterministically (ADP value, roster needs,
round-phase rules). Your ONLY job is to EXPLAIN it and add nuance: player trend,
situation, what the roster gains. You never change the decision.

FORBIDDEN: never mention specific injury diagnoses, body parts, or "chronic"
language.

OUTPUT FORMAT (JSON only, no markdown fences):
{
  "reasoning": "1-2 sentences explaining the engine's decision, with trend/situation nuance",
  "confidence": "high" | "medium" | "low"
}
"""


_SNAKE_YOUR_TURN_PROMPT = """You are an expert fantasy football snake draft analyst.

THE RECOMMENDATION ENGINE HAS ALREADY PICKED the player to draft — chosen
deterministically from roster needs, ADP value, urgency (adp_diff), and
round-phase rules. Your ONLY job is to EXPLAIN the pick and add nuance: the
player's situation, trend, why he fits this roster now. You never change the
pick and never recommend a different player.

FORBIDDEN: never mention specific injury diagnoses, body parts, or "chronic"
language.

OUTPUT FORMAT (JSON only, no markdown fences):
{
  "reasoning": "1-2 sentences explaining the engine's pick, with trend/situation nuance",
  "confidence": "high|medium|low"
}
"""


async def _log_draft_usage(response) -> None:
    """Log a live-draft Sonnet call to api_usage_log. Draft calls previously
    bypassed usage logging entirely — draft day (the biggest spend day) was
    invisible in the cost dashboard. Never raises: a logging failure must not
    break a live draft."""
    try:
        from datetime import datetime, timezone
        from decimal import Decimal

        from backend.agents.base_agent import (
            SONNET_INPUT_PER_MTK, SONNET_OUTPUT_PER_MTK,
        )
        from backend.database import AsyncSessionLocal
        from backend.models.api_usage_log import ApiUsageLog

        usage = response.usage
        cost = Decimal(str(
            usage.input_tokens * SONNET_INPUT_PER_MTK / 1_000_000
            + usage.output_tokens * SONNET_OUTPUT_PER_MTK / 1_000_000
        ))
        async with AsyncSessionLocal() as session:
            session.add(ApiUsageLog(
                agent_name="live_draft",
                model=SONNET,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                estimated_cost_usd=cost,
                cache_hit=False,
                entity_id="draft",
                called_at=datetime.now(timezone.utc),
            ))
            await session.commit()
    except Exception as exc:  # pragma: no cover — never let logging break a draft
        logger.warning("draft usage logging failed: %s", exc)


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
        # Draft-scoped client WITH SDK retries (the shared client is
        # max_retries=0 for BaseAgent's own backoff; this path has none).
        self._client = get_client().with_options(max_retries=_DRAFT_CLIENT_RETRIES)
        self.last_recommendation: dict | None = None
        # In-flight async Sonnet enrichment tasks (fire-and-forget with a handle
        # so tests/verifiers can await completion via wait_for_ai()).
        self._ai_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Async AI enrichment plumbing (deterministic-first)
    # ------------------------------------------------------------------

    def _spawn_ai_task(self, coro) -> None:
        """Fire-and-forget the Sonnet enrichment. Failures are logged, never
        raised — the deterministic rec is already on screen."""
        task = asyncio.create_task(coro)
        self._ai_tasks.add(task)
        task.add_done_callback(self._ai_tasks.discard)

    async def wait_for_ai(self) -> None:
        """Await all in-flight enrichment tasks (tests / graceful shutdown)."""
        if self._ai_tasks:
            await asyncio.gather(*list(self._ai_tasks), return_exceptions=True)

    async def _call_sonnet_explain(
        self, system: str, user: str, max_tokens: int = 300,
    ) -> dict:
        """One retried Sonnet call, usage-logged to api_usage_log. Returns the
        parsed {reasoning, confidence} dict; raises on unrecoverable API failure
        (caller decides what a failure means — usually: keep the deterministic
        text)."""
        response = await self._client.messages.create(
            model=SONNET,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        await _log_draft_usage(response)
        data = parse_json_output(response.content[0].text)
        if not isinstance(data, dict):
            raise ValueError("explain output was not a JSON object")
        return data

    async def _broadcast_enrichment(
        self, base_rec: dict, system: str, user: str,
    ) -> None:
        """Call Sonnet to EXPLAIN the already-broadcast deterministic rec, then
        re-broadcast the SAME recommendation with enriched reasoning. The player
        and every number are locked — only reasoning/confidence change. Skipped
        (with a log line, never an error) if the model fails or a newer
        recommendation has superseded this one."""
        try:
            data = await self._call_sonnet_explain(system, user)
        except Exception as exc:
            logger.warning(
                "AI enrichment failed for %s (%s) — deterministic rec stands",
                base_rec.get("player_name"), exc,
            )
            return

        # Staleness guard: if a newer rec was broadcast while Sonnet was
        # thinking, do not clobber it with stale nuance.
        current = self.last_recommendation or {}
        if current.get("player_name") != base_rec.get("player_name"):
            logger.info(
                "AI enrichment for %s superseded by a newer rec — dropped",
                base_rec.get("player_name"),
            )
            return

        enriched = {
            **base_rec,
            "reasoning": str(data.get("reasoning") or base_rec.get("reasoning", "")),
            "confidence": data.get("confidence", base_rec.get("confidence", "medium")),
            "ai_enriched": True,
        }
        self.last_recommendation = enriched
        await self.ws_manager.broadcast(enriched)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def handle_event(self, event: dict) -> None:
        """Route a bridge event to the appropriate handler."""
        event_type = event.get("type")
        if event_type == "nomination":
            await self.on_nomination(event)
        elif event_type == "your_turn":
            await self.on_your_turn(event)
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

        # Step 7: DETERMINISTIC decision — the engine decides action + number
        # (roster-aware). Sonnet never does; it only explains, async, below.
        budget_allows_block = spendable >= max_block_value > 0
        action, bid_ceiling, det_reasoning = self._deterministic_auction_action(
            record, live_ceiling, spendable, max_block_value, budget_allows_block,
        )

        context = {
            "player_name": record["name"],
            "position": record["position"],
            "team": record.get("team_abbr", ""),
            "tier": record.get("tier"),
            "system_value": record.get("system_value", 0),
            "market_value": record.get("market_value", 0),
            "pre_computed_ceiling": live_ceiling,
            "engine_decision": {"action": action, "bid_ceiling": bid_ceiling,
                                "basis": det_reasoning},
            "roster_needs": sorted(self.state.need_positions(self._your_roster_dicts())),
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
            "budget_allows_block": budget_allows_block,
            "opponent_alerts": opponent_alerts,
            "notes": record.get("notes", ""),
            "pay_up_flag": record.get("pay_up_flag", False),
            "value_assessment": record.get("value_assessment", ""),
            "manager_styles": manager_styles,
        }

        # Step 8: BROADCAST THE DETERMINISTIC REC IMMEDIATELY (no model wait).
        elapsed = (time.monotonic() - start) * 1000
        message = {
            "type": "recommendation",
            "action": action,
            "bid_ceiling": bid_ceiling,
            "reasoning": det_reasoning,
            "confidence": "high",       # deterministic constraints — not a guess
            "ai_enriched": False,
            "player_name": record["name"],
            "position": record.get("position", ""),
            "injury_status": record.get("injury_status"),
            "system_value": record.get("system_value", 0),
            "market_value": record.get("market_value", 0),
            "pre_computed_ceiling": live_ceiling,
            "active_flags": context["active_flags"],
            "opponent_alerts": opponent_alerts,
            "block_value": max_block_value,
            "budget_allows_block": budget_allows_block,
            "budget_summary": {
                "your_remaining": self.state.get_your_remaining_budget(),
                "spendable_on_this_player": spendable,
                "minimum_completion_budget": self.state.get_minimum_completion_budget(),
                "roster_slots_remaining": self.state.get_roster_slots_remaining(),
            },
            "elapsed_ms": round(elapsed),
        }
        self.last_recommendation = message
        await self.ws_manager.broadcast(message)
        logger.info(
            "Deterministic rec for %s in %.0fms: %s $%s (AI nuance async)",
            record["name"], elapsed, action, bid_ceiling,
        )

        # Step 9: async Sonnet ENRICHMENT — explains the decision, never alters it.
        self._spawn_ai_task(self._broadcast_enrichment(
            message, _SYSTEM_PROMPT, json.dumps(context, default=str),
        ))

    def _your_roster_dicts(self) -> list[dict]:
        """Your auction roster as the [{'position': ...}] shape the needs
        primitives consume (snake uses get_my_roster(), which already is)."""
        return [{"position": p.position} for p in self.state.your_roster]

    def _deterministic_auction_action(
        self,
        record: dict,
        live_ceiling: int,
        spendable: int,
        max_block_value: float,
        budget_allows_block: bool,
    ) -> tuple[str, int, str]:
        """THE ENGINE'S auction decision — (action, bid_ceiling, reasoning).

        Pure Python: budget feasibility (live_ceiling already embeds spendable),
        ROSTER NEEDS (the '$60 on three RBs' fix — a position with no open
        starter slot and no free bench room is a pass, however good the value),
        block opportunities, and the pre-computed value signals.
        """
        pos = record.get("position", "")
        roster = self._your_roster_dicts()
        needs = self.state.need_positions(roster)
        open_need_slots = sum(self.state.get_unfilled_needs(roster).values())
        slots_remaining = self.state.get_roster_slots_remaining()
        free_bench = slots_remaining - open_need_slots

        # 1. Feasibility — cannot bid at all.
        if live_ceiling <= 0 or spendable <= 0:
            return ("pass", 0,
                    "Cannot bid — any bid would leave you unable to fill your "
                    "remaining roster slots at $1 each.")

        # 2. Roster fit — position already filled and every remaining slot is
        #    reserved for unfilled needs: stacking it would strand a need.
        if pos and pos not in needs and free_bench <= 0:
            return ("pass", 0,
                    f"{pos} is already filled and your {slots_remaining} remaining "
                    f"slot(s) are reserved for unfilled needs "
                    f"({', '.join(sorted(needs)) or 'none'}).")

        # 3. Block — denying an opponent combo is worth more than personal value.
        if budget_allows_block and max_block_value > live_ceiling:
            ceiling = max(1, min(int(round(max_block_value)), spendable))
            return ("block", ceiling,
                    f"Block bid — denying the opponent combo is worth ${ceiling}, "
                    f"above his ${live_ceiling} value to your roster.")

        # 4. Value action — buy (pursue) vs bid_to (monitor with a drop point).
        depth_note = (
            "" if pos in needs
            else " (depth add — position starters are filled)"
        )
        system_v = float(record.get("system_value") or 0)
        market_v = float(record.get("market_value") or 0)
        if record.get("pay_up_flag") or (system_v > market_v and pos in needs):
            return ("buy", live_ceiling,
                    f"Strong value at a roster need — pursue up to the "
                    f"${live_ceiling} ceiling{depth_note}.")
        return ("bid_to", live_ceiling,
                f"Fair target — bid up to ${live_ceiling}, drop out "
                f"above it{depth_note}.")

    async def on_pick_confirmed(self, event: dict) -> None:
        """Update state after every confirmed pick."""
        pick = DraftPick(
            player_id=event.get("player_id", ""),
            team_id=event.get("team_id", ""),
            price=event.get("final_price", 0),
            player_name=event.get("player_name", ""),
            position=event.get("position", ""),
        )
        self.state.record_pick(pick, is_yours=bool(event.get("is_yours")))

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

        LOOKUP-SAFE (two real failure modes, both previously a silent no-rec):
          * a None/empty id renders as ``yahoo_player_id IS NULL`` in SQLAlchemy
            and matched ~2,758 rows -> MultipleResultsFound. Guarded: empty id
            returns None (unknown player) immediately.
          * genuinely duplicated ids: prefer the ranked (adp_rank) / valued row,
            loud-warn, never crash (scripts/dedupe_yahoo_player_ids.py cleans).
        """
        if not yahoo_player_id:
            logger.warning(
                "player lookup with empty yahoo_player_id — treating as unknown "
                "player (no IS NULL scan)",
            )
            return None
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
            players = result.scalars().all()

        if not players:
            return None
        if len(players) > 1:
            logger.warning(
                "DUPLICATE yahoo_player_id %s: %d rows (%s) — using the ranked/"
                "valued one; run scripts/dedupe_yahoo_player_ids.py",
                yahoo_player_id, len(players), [p.name for p in players],
            )
            players = sorted(
                players,
                key=lambda p: (
                    p.adp_rank is None,                    # ranked rows first
                    p.baseline_value is None,              # then valued rows
                    -(float(p.baseline_value or 0)),
                ),
            )
        player = players[0]

        # PER-FORMAT ADP OVERLAY (Phase 2). The draft board is a PRE-DRAFT surface, so
        # its scoring-dependent signal (ADP + tier) reads the league-format row from
        # player_format_values — NOT a live re-score (that line is for in-season tools).
        # PPR is byte-identical: the PPR row equals the players table, and PPR skips the
        # overlay entirely. Missing per-format ADP (pipeline hasn't populated it) → keep
        # the players-table (PPR) ADP and DISCLOSE the fallback. adp_ai (the AI snake pick)
        # has no per-format row yet — it stays PPR-anchored, noted in the disclosure.
        scoring_format = getattr(self.state, "scoring_format", "ppr") or "ppr"
        fmt_adp_fp: float | None = None
        fmt_tier: int | None = None
        fmt_adp_defaulted = False
        if scoring_format != "ppr":
            async with self._db_session_factory() as session:
                fmt_row = (await session.execute(
                    select(PlayerFormatValues).where(
                        PlayerFormatValues.player_id == player.id,
                        PlayerFormatValues.scoring_format == scoring_format,
                    )
                )).scalar_one_or_none()
            if fmt_row is not None:
                if fmt_row.tier is not None:
                    fmt_tier = fmt_row.tier
                if fmt_row.adp_fantasypros is not None:
                    fmt_adp_fp = float(fmt_row.adp_fantasypros)
            # No per-format market ADP available → the ADP shown is still PPR.
            fmt_adp_defaulted = fmt_adp_fp is None

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
            "injury_status": player.injury_status,
            # Tier reads the league-format row when available (a reception-dependent
            # player can tier-fall in Standard); PPR / missing row → players-table tier.
            "tier": fmt_tier if fmt_tier is not None else player.tier,
            "system_value": float(player.baseline_value or 0),
            "market_value": float(player.market_value or 0),
            "ai_bid_ceiling": player.ai_bid_ceiling,
            "recommended_bid_ceiling": float(player.recommended_bid_ceiling or 0),
            # Pre-draft availability discount (engines/availability.py): the live bid
            # ceiling is prorated by this for a known multi-week absence (base × factor).
            "availability_factor": float(player.availability_factor) if player.availability_factor is not None else 1.0,
            # ADP (snake path) — null until a pipeline run populates them. adp_fantasypros
            # is the FORMAT-MATCHED market ADP (from player_format_values) when populated,
            # else the players-table PPR value (fmt_adp_defaulted flags the fallback).
            "adp_ai": float(player.adp_ai) if player.adp_ai is not None else None,
            "adp_fantasypros": (
                fmt_adp_fp if fmt_adp_fp is not None
                else (float(player.adp_fantasypros) if player.adp_fantasypros is not None else None)
            ),
            "adp_scoring": scoring_format if fmt_adp_fp is not None else player.adp_scoring,
            "scoring_format": scoring_format,
            # True when a non-PPR league is seeing PPR ADP because the per-format market
            # ADP isn't populated yet (UI discloses "showing PPR ADP"). adp_ai stays PPR.
            "adp_format_defaulted": fmt_adp_defaulted,
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
        # Start from best available pre-computed ceiling, prorated by pre-draft
        # AVAILABILITY (a known multi-week absence discounts the live bid ceiling — a
        # stud on PUP/long-IR is worth less at auction; deterministic, base × factor).
        base_ceiling = float(
            record.get("ai_bid_ceiling")
            or record.get("recommended_bid_ceiling")
            or record.get("system_value")
            or 1
        ) * float(record.get("availability_factor", 1.0) or 1.0)

        # Apply dependency flag modifier
        if flag_modifier != 0:
            adjusted = base_ceiling * (1.0 + flag_modifier)
        else:
            adjusted = base_ceiling

        # Position cap
        pos = record.get("position", "")
        max_bid = MAX_REALISTIC_BID.get(pos, 80)

        # Budget constraint. FEASIBILITY FIX: when spendable is 0 (any bid would
        # strand the roster), the ceiling is 0 — "cannot bid", not a floored $1
        # infeasible bid (the old max(1, …) recommended $1 with $0 spendable in
        # end-game states; Sonnet was papering over it).
        if spendable <= 0:
            return 0
        ceiling = min(adjusted, spendable, max_bid)
        return max(1, min(int(round(ceiling)), spendable))

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
        """Snake nomination: THE ENGINE DECIDES DRAFT/WAIT deterministically
        (ADP value vs current pick, roster needs, round-phase guardrails) and
        broadcasts instantly; Sonnet explains async. (Defensive path — snake
        pollers emit your_turn, not nomination.)"""
        start = time.monotonic()
        player_id = event.get("player_id", "")

        record = await self._get_player_record(player_id)
        if not record:
            await self._emit_unknown_player(player_id, event)
            return

        current_pick = event.get("current_pick")
        roster = self.state.get_my_roster()
        needs = self.state.need_positions(roster)
        pos = record.get("position") or ""
        adp_ai = record.get("adp_ai")
        adp_fp = record.get("adp_fantasypros")
        team_count = self.state.league_config.team_count or 12
        round_num = ((int(current_pick) - 1) // team_count + 1) if current_pick else None
        total_rounds = self.state.league_config.total_roster_size

        # DETERMINISTIC DRAFT/WAIT — guardrails first, then need + value.
        action, why = "wait", "Better value or a more urgent need is likely available."
        if round_num is not None and pos in ("K", "DEF") and round_num <= total_rounds - KDEF_FINAL_ROUNDS:
            why = f"Round-phase rule: {pos} only in the final {KDEF_FINAL_ROUNDS} rounds."
        elif round_num is not None and pos == "QB" and round_num < QB_MIN_ROUND:
            why = f"Round-phase rule: no QB before round {QB_MIN_ROUND} — the position is deep."
        elif pos in needs and (adp_ai is None or current_pick is None
                               or float(adp_ai) <= float(current_pick) + 6):
            action = "draft"
            why = f"Fills your open {pos} slot at fair ADP value."
        elif adp_ai is not None and current_pick is not None and float(adp_ai) < float(current_pick) - 6:
            action = "draft"
            why = "Well below his AI ADP — value outweighs the filled position."
        elif pos not in needs:
            why = f"{pos} starters are already filled — hold for an open need."

        # FORMAT-MATCHED AVAILABILITY (Phase 2, non-PPR ONLY → PPR byte-identical). The
        # market drafts reception-dependent players LATER in Half/Standard, so if this
        # player's format-matched market ADP falls well past the current pick, he'll still
        # be here next time around — advise WAIT even for a need (the "take-now in PPR,
        # wait in Standard" case). Only fires when a per-format market ADP is populated.
        scoring_format = getattr(self.state, "scoring_format", "ppr") or "ppr"
        if (action == "draft" and scoring_format != "ppr"
                and not record.get("adp_format_defaulted")
                and adp_fp is not None and current_pick is not None
                and float(adp_fp) > float(current_pick) + SNAKE_FORMAT_WAIT_MARGIN):
            action = "wait"
            why = (f"In {scoring_format.replace('_', ' ')} his market ADP (~{float(adp_fp):.0f}) "
                   f"falls well past pick {int(current_pick)} — he'll be here later, you can wait.")

        adp_diff = None
        if adp_ai is not None and adp_fp is not None:
            adp_diff = round(float(adp_fp) - float(adp_ai), 1)

        rec = {
            "type": "recommendation",
            "action": action,
            "reasoning": why,
            "player_name": record["name"],
            "position": pos,
            "injury_status": record.get("injury_status"),
            "adp_ai": adp_ai,
            "adp_fp": adp_fp,
            "adp_diff": adp_diff,
            "position_need": "high" if pos in needs else "low",
            "confidence": "high",
            "ai_enriched": False,
            "tier": record.get("tier"),
            # Phase 2: the format the ADP/tier were read in + disclosure when a non-PPR
            # league is seeing PPR ADP (per-format market ADP not populated / adp_ai is PPR).
            "scoring_format": scoring_format,
            "adp_scoring": record.get("adp_scoring"),
            "adp_format_defaulted": bool(record.get("adp_format_defaulted")),
            "elapsed_ms": round((time.monotonic() - start) * 1000),
        }
        self.last_recommendation = rec
        await self.ws_manager.broadcast(rec)
        logger.info("Deterministic snake rec for %s in %dms: %s (AI nuance async)",
                    record["name"], rec["elapsed_ms"], action)

        context = {
            "engine_decision": {"action": action, "basis": why},
            "player_name": record["name"], "position": pos,
            "team": record.get("team_abbr"), "tier": record.get("tier"),
            "adp_ai": adp_ai, "adp_fantasypros": adp_fp,
            "current_pick": current_pick,
            "scoring_format": self.state.scoring_format,
            "my_roster": self.state.get_roster_summary(),
            "availability_risk": record.get("availability_risk"),
            "projected_ppr": record.get("projected_ppr"),
        }
        self._spawn_ai_task(self._broadcast_enrichment(
            rec, _SNAKE_SYSTEM_PROMPT, self._build_snake_prompt(context),
        ))

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
            f"ENGINE DECISION (final): {json.dumps(context.get('engine_decision', {}))}\n"
            f"Explain this decision in 1-2 sentences with trend/situation nuance."
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

    # ------------------------------------------------------------------
    # Snake — user on the clock (best-available recommendation)
    # ------------------------------------------------------------------

    async def on_your_turn(self, event: dict) -> None:
        """User is on the clock in a snake draft.

        DETERMINISTIC-FIRST: the ENGINE picks the player (roster needs + ADP
        value + urgency + round-phase guardrails — pure Python, instant) and
        broadcasts immediately. Sonnet is then called async purely to explain
        the pick; it never changes it. A model failure = the deterministic rec
        simply keeps its deterministic reasoning text.
        """
        start = time.monotonic()
        round_num = event.get("round")
        pick_num = event.get("pick")

        available = await self._get_top_available()
        if not available:
            message = {
                "type": "recommendation",
                "action": "wait",
                "reasoning": "No ranked players available — manual evaluation needed.",
                "confidence": "low",
                "round": round_num,
                "pick": pick_num,
                "elapsed_ms": 0,
            }
            self.last_recommendation = message
            await self.ws_manager.broadcast(message)
            return

        pick, why, position_need = self._deterministic_your_turn_pick(
            available, round_num,
        )
        adp_diff = pick.get("adp_diff")
        can_wait = adp_diff is not None and float(adp_diff) > 10

        elapsed = (time.monotonic() - start) * 1000
        rec = {
            "type": "recommendation",
            "action": "draft",
            "player_name": pick.get("name"),
            "reasoning": why,
            "adp_rank": pick.get("adp_rank"),
            "adp_fp": pick.get("adp_fp"),
            "adp_diff": adp_diff,
            "can_wait": bool(can_wait),
            "wait_until_pick": None,
            "confidence": "high",
            "ai_enriched": False,
            "position": pick.get("position"),
            "position_need": position_need,
            "round": round_num,
            "pick": pick_num,
            "elapsed_ms": round(elapsed),
        }
        self.last_recommendation = rec
        await self.ws_manager.broadcast(rec)
        logger.info(
            "Deterministic your-turn rec (R%s P%s) in %.0fms: %s (AI nuance async)",
            round_num, pick_num, elapsed, pick.get("name"),
        )

        # Async Sonnet ENRICHMENT — explain the engine's pick, never change it.
        context = {
            "round": round_num,
            "pick": pick_num,
            "my_roster": self.state.get_my_roster(),
            "top_available": available[:15],
            "engine_pick": {"player_name": pick.get("name"),
                            "position": pick.get("position"), "basis": why},
        }
        self._spawn_ai_task(self._broadcast_enrichment(
            rec, _SNAKE_YOUR_TURN_PROMPT, self._build_your_turn_prompt(context),
        ))

    def _deterministic_your_turn_pick(
        self, available: list[dict], round_num,
    ) -> tuple[dict, str, str]:
        """THE ENGINE'S snake pick — (player, reasoning, position_need).

        Rules (each previously outsourced to the prompt; now hard code):
          1. ROUND-PHASE GUARDRAILS: no K/DEF outside the final KDEF_FINAL_ROUNDS
             rounds; no QB before QB_MIN_ROUND. (Sonnet violated its own QB
             framework twice in recon — constraints live in code.)
          2. NEED-AWARE SELECTION: reuse get_unfilled_needs()/need_positions()
             (the same single needs implementation the prompt already used).
             A BPA that still adds STARTER value is by definition a need pick —
             take it. A bench-only BPA never beats a real need unless the need
             reach is absurd (> NEED_RANK_WINDOW ranks).
          3. URGENCY TIEBREAK: among near-equal need candidates, prefer one who
             will NOT last (adp_diff <= 10) over one we can wait on.
        """
        roster = self.state.get_my_roster()
        needs = self.state.need_positions(roster)
        total_rounds = self.state.league_config.total_roster_size
        rnd = int(round_num) if round_num else 1

        kdef_ok = rnd > total_rounds - KDEF_FINAL_ROUNDS
        qb_ok = rnd >= QB_MIN_ROUND

        def _eligible(p: dict) -> bool:
            pos = p.get("position")
            if pos in ("K", "DEF") and not kdef_ok:
                return False
            if pos == "QB" and not qb_ok:
                return False
            return True

        eligible = [p for p in available if _eligible(p)] or list(available)
        bpa = eligible[0]

        need_candidates = [p for p in eligible if p.get("position") in needs]

        # BPA at a need position IS the need pick.
        if bpa.get("position") in needs:
            return (bpa,
                    f"Best available (AI rank {bpa.get('adp_rank')}) and fills "
                    f"your open {bpa.get('position')} slot.",
                    "high")

        if not need_candidates:
            return (bpa,
                    "All starter needs filled — best available by ADP for depth.",
                    "low")

        best_need = need_candidates[0]
        # Urgency tiebreak: within a small band of the best need candidate,
        # prefer the one the market will take first (not can_wait).
        band = [p for p in need_candidates
                if (p.get("adp_rank") or 0) - (best_need.get("adp_rank") or 0) <= 12]
        if (best_need.get("adp_diff") or 0) > 10:
            urgent = [p for p in band if (p.get("adp_diff") or 0) <= 10]
            if urgent:
                best_need = urgent[0]

        gap = (best_need.get("adp_rank") or 0) - (bpa.get("adp_rank") or 0)
        if gap > NEED_RANK_WINDOW:
            return (bpa,
                    f"Best need option is {gap} ranks below the board's best — "
                    f"too far a reach; take value now.",
                    "medium")

        return (best_need,
                f"Fills your open {best_need.get('position')} slot — the board's "
                f"best ({bpa.get('name')}) adds no starter value to this roster.",
                "high")

    async def _get_top_available(self) -> list[dict]:
        """Top available players by adp_rank, excluding drafted players.

        Excludes by NAME (state.is_drafted), since the snake pick id is a
        Yahoo-internal id that doesn't match our DB yahoo_player_id.

        K/DEF are included (they carry adp_rank from the T1 static pass, ranking
        near the bottom at ~460+), so a kicker/defense surfaces in the LATE rounds
        instead of the list going empty when only K/DEF remain. We can't cap the
        query with a small LIMIT: the old limit(60) only ever fetched ranks 1-60,
        so the late-round available pool (and K/DEF specifically) was never
        reachable once those were drafted. Instead we scan by ascending rank and
        stop once TOP_N undrafted are collected — the fetch is bounded by the
        ranked pool (~720) and the scan short-circuits, so early rounds cost the
        same and late rounds walk deeper until the real best-available appear.
        """
        TOP_N = 20
        async with self._db_session_factory() as session:
            stmt = (
                select(Player)
                .where(
                    Player.adp_rank.isnot(None),
                    Player.position.in_(["QB", "RB", "WR", "TE", "K", "DEF"]),
                )
                .order_by(Player.adp_rank.asc())
            )
            result = await session.execute(stmt)
            players = result.scalars().all()

        out: list[dict] = []
        for p in players:
            if self.state.is_drafted(p.name):
                continue
            out.append({
                "name": p.name,
                "position": p.position,
                "team": p.team_abbr,
                "injury_status": p.injury_status,
                "adp_rank": p.adp_rank,
                "adp_fp": (
                    float(p.adp_fantasypros)
                    if p.adp_fantasypros is not None else None
                ),
                "adp_diff": float(p.adp_diff) if p.adp_diff is not None else None,
                "snake_flag": p.snake_flag,
                "tier": p.tier,
            })
            if len(out) >= TOP_N:
                break
        return out

    def _format_my_roster(self, roster: list[dict]) -> str:
        if not roster:
            return "No picks yet"
        return "\n".join(
            f"  R{p.get('round') if p.get('round') is not None else '?'}: "
            f"{p.get('player_name')} ({p.get('position') or '?'})"
            for p in roster
        )

    def _build_your_turn_prompt(self, context: dict) -> str:
        my_roster = context["my_roster"]

        lines = []
        for i, p in enumerate(context["top_available"], start=1):
            diff = p.get("adp_diff")
            diff_str = f"{diff:+.0f}" if diff is not None else "n/a"
            lines.append(
                f"  {i}. {p['name']} ({p['position']}) "
                f"AI:{p['adp_rank']} FP:{p.get('adp_fp')} "
                f"diff:{diff_str} [{p.get('snake_flag') or 'n/a'}]"
            )
        avail_str = "\n".join(lines)
        roster_str = self._format_my_roster(my_roster)
        needs_str = self.state.format_roster_needs(my_roster)

        return (
            f"YOU ARE ON THE CLOCK\n"
            f"Round: {context['round']}\n"
            f"Pick: {context['pick']}\n\n"
            f"YOUR ROSTER ({len(my_roster)} picks):\n{roster_str}\n\n"
            f"POSITIONS STILL NEEDED:\n{needs_str}\n\n"
            f"TOP AVAILABLE (by AI ADP):\n{avail_str}\n\n"
            f"ENGINE PICK (final): {json.dumps(context.get('engine_pick', {}))}\n"
            f"Explain this pick in 1-2 sentences with trend/situation nuance. "
            f"Do not recommend a different player."
        )
