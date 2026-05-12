"""
BaseAgent — foundation for all pre-draft pipeline agents.

Every pipeline agent extends BaseAgent and calls self.call_once().
The iterative tool-use loop (run_agent) lives in agent_loop.py
and is ONLY imported by live_draft.py.

Usage:
    class TeamSystemsAgent(BaseAgent):
        AGENT_NAME = "team_systems"
        AGENT_MODEL = "claude-haiku-4-5-20251001"
        AGENT_MAX_TOKENS = 500

        async def run_for_team(self, team: str) -> dict | None:
            context = await self._build_team_context(team)
            raw = await self.call_once(
                system=SYSTEM_PROMPT,
                user=json.dumps(context),
                input_data=context,
                entity_id=team,
            )
            return parse_json_output(raw)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime, timezone
from decimal import Decimal

import anthropic

from backend.config import settings
from backend.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model strings — only these two values exist in this project
# ---------------------------------------------------------------------------

HAIKU  = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Pricing constants — update if Anthropic changes pricing
# ---------------------------------------------------------------------------

HAIKU_INPUT_PER_MTK   = 0.80   # per million tokens
HAIKU_OUTPUT_PER_MTK  = 4.00
SONNET_INPUT_PER_MTK  = 3.00
SONNET_OUTPUT_PER_MTK = 15.00

_MODEL_PRICING: dict[str, tuple[float, float]] = {
    HAIKU:  (HAIKU_INPUT_PER_MTK,  HAIKU_OUTPUT_PER_MTK),
    SONNET: (SONNET_INPUT_PER_MTK, SONNET_OUTPUT_PER_MTK),
}

# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=0,  # We handle retries via _call_with_backoff
        )
    return _client


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------

class BaseAgent:
    """
    Base class for all pre-draft pipeline agents.

    Subclasses MUST declare these class attributes — no defaults:
        AGENT_NAME:       str  — unique snake_case name for this agent
        AGENT_MODEL:      str  — one of HAIKU or SONNET
        AGENT_MAX_TOKENS: int  — hard ceiling per COST_RULES.md

    The call_once() method handles caching and usage logging transparently.
    Subclasses never call messages.create() directly.
    """

    AGENT_NAME: str        # required — no default
    AGENT_MODEL: str       # required — no default
    AGENT_MAX_TOKENS: int  # required — no default

    def __init__(self, dry_run: bool = False, warehouse=None):
        for attr in ("AGENT_NAME", "AGENT_MODEL", "AGENT_MAX_TOKENS"):
            if not getattr(type(self), attr, None):
                raise ValueError(f"{type(self).__name__} must declare {attr}")
        self.dry_run = dry_run
        self._warehouse = warehouse
        self._client = get_client()

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------

    async def call_once(
        self,
        system: str,
        user: str,
        input_data: dict,
        entity_id: str = "",
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        Single API call with transparent caching and usage logging.

        Args:
            model: Override class AGENT_MODEL for this call (e.g. Sonnet for complex players).
            max_tokens: Override class AGENT_MAX_TOKENS for this call.

        Steps:
          1. Hash input_data with sha256
          2. Check agent_cache — if hit, log cache_hit=True and return cached text
          3. If dry_run=True, log estimate and return ""
          4. Call client.messages.create() with effective model and max_tokens
          5. Log to api_usage_log (cache_hit=False)
          6. Write raw response text to agent_cache
          7. Return response text

        The caller is responsible for parsing the returned string (JSON, etc.).
        """
        effective_model = model or self.AGENT_MODEL
        effective_max = max_tokens or self.AGENT_MAX_TOKENS

        input_hash = _hash_input(input_data)

        # 1. Cache check
        cached = await self._check_cache(input_hash, entity_id)
        if cached is not None:
            await self._log_usage(
                input_tokens=0,
                output_tokens=0,
                cache_hit=True,
                entity_id=entity_id,
                model_override=effective_model,
            )
            logger.info("Cache hit: %s / %s", self.AGENT_NAME, entity_id)
            return cached

        # 2. Dry run
        if self.dry_run:
            in_price, out_price = _MODEL_PRICING.get(
                effective_model, (SONNET_INPUT_PER_MTK, SONNET_OUTPUT_PER_MTK)
            )
            est_input_tokens = len(user) // 4  # rough: ~4 chars per token
            est_cost = (
                est_input_tokens * in_price / 1_000_000
                + effective_max * out_price / 1_000_000
            )
            logger.info(
                "[DRY RUN] %s / %s — model=%s, est. %d input tokens, $%.5f",
                self.AGENT_NAME, entity_id, effective_model,
                est_input_tokens, est_cost,
            )
            return ""

        # 3. Real API call (with exponential backoff for 529 / rate limits)
        response = await self._call_with_backoff(
            model=effective_model,
            max_tokens=effective_max,
            system=system,
            user=user,
        )

        raw = response.content[0].text

        # 4. Log + cache
        await self._log_usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_hit=False,
            entity_id=entity_id,
            model_override=effective_model,
        )
        await self._write_cache(input_hash, raw, entity_id)

        return raw

    # ------------------------------------------------------------------
    # API call with exponential backoff
    # ------------------------------------------------------------------

    async def _call_with_backoff(
        self,
        model: str,
        max_tokens: int,
        system: str,
        user: str,
        max_retries: int = 6,
    ) -> anthropic.types.Message:
        """Call messages.create() with exponential backoff for 529 / rate limits.

        Retry schedule (base * 2^attempt + jitter):
          429 RateLimitError:  5s, 10s, 20s, 40s, 80s, 160s
          529 Overloaded:     10s, 20s, 40s, 80s, 160s, 320s
        """
        for attempt in range(max_retries):
            try:
                return await self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )

            except anthropic.RateLimitError:
                if attempt == max_retries - 1:
                    raise
                wait = (2 ** attempt) * 5 + random.uniform(0, 2)
                logger.warning(
                    "Rate limited (attempt %d/%d) — waiting %.1fs",
                    attempt + 1, max_retries, wait,
                )
                await asyncio.sleep(wait)

            except anthropic.APIStatusError as e:
                if e.status_code == 529:
                    if attempt == max_retries - 1:
                        raise
                    wait = (2 ** attempt) * 10 + random.uniform(0, 5)
                    logger.warning(
                        "API overloaded 529 (attempt %d/%d) — waiting %.1fs",
                        attempt + 1, max_retries, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    async def _check_cache(self, input_hash: str, entity_id: str) -> str | None:
        from sqlalchemy import select
        from backend.models.agent_cache import AgentCache

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AgentCache).where(
                    AgentCache.agent_name == self.AGENT_NAME,
                    AgentCache.entity_id == entity_id,
                    AgentCache.input_hash == input_hash,
                )
            )
            hit = result.scalar_one_or_none()
            if hit is None:
                return None
            # Touch timestamp so pipeline status reflects last run time
            hit.created_at = datetime.now(timezone.utc)
            await session.commit()
            return hit.output_json

    async def _write_cache(self, input_hash: str, output: str, entity_id: str) -> None:
        from backend.models.agent_cache import AgentCache
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        async with AsyncSessionLocal() as session:
            stmt = (
                pg_insert(AgentCache)
                .values(
                    agent_name=self.AGENT_NAME,
                    entity_id=entity_id,
                    input_hash=input_hash,
                    output_json=output,
                    created_at=datetime.now(timezone.utc),
                )
                .on_conflict_do_nothing(constraint="uq_agent_cache_key")
            )
            await session.execute(stmt)
            await session.commit()

    # ------------------------------------------------------------------
    # Usage logging
    # ------------------------------------------------------------------

    async def _log_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_hit: bool,
        entity_id: str,
        model_override: str | None = None,
    ) -> None:
        from backend.models.api_usage_log import ApiUsageLog

        effective_model = model_override or self.AGENT_MODEL
        in_price, out_price = _MODEL_PRICING.get(
            effective_model, (SONNET_INPUT_PER_MTK, SONNET_OUTPUT_PER_MTK)
        )
        cost = Decimal(str(
            input_tokens * in_price / 1_000_000
            + output_tokens * out_price / 1_000_000
        ))

        async with AsyncSessionLocal() as session:
            session.add(ApiUsageLog(
                agent_name=self.AGENT_NAME,
                model=effective_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                cache_hit=cache_hit,
                entity_id=entity_id,
                called_at=datetime.now(timezone.utc),
            ))
            await session.commit()

        if not cache_hit and (input_tokens or output_tokens):
            logger.info(
                "%s / %s — %d in + %d out tokens, est. $%.5f",
                self.AGENT_NAME, entity_id,
                input_tokens, output_tokens, float(cost),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_input(data: dict) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def parse_json_output(raw: str) -> dict | list:
    """
    Parse JSON from model output, tolerating:
    - Markdown fences (```json ... ```) — anywhere in the response
    - Prose preamble before the JSON (model ignoring JSON-only instruction)
    - Truncated arrays from hitting max_tokens (returns completed elements only)
    """
    raw = raw.strip()

    # Strip markdown fences — handle fences anywhere in the response
    if "```" in raw:
        import re
        # Extract content from first fenced block: ```json\n...\n```
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
        if fence_match:
            raw = fence_match.group(1).strip()

    # Fast path — clean response
    if raw.startswith(("[", "{")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # May be truncated — fall through to recovery
            pass
    else:
        # Model added prose preamble — find first [ or {
        bracket = min(
            (raw.find(c) for c in ("[", "{") if raw.find(c) != -1),
            default=-1,
        )
        if bracket != -1:
            raw = raw[bracket:]
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass  # Truncated — fall through to recovery

    # Recovery: extract complete JSON objects from the text
    # Works for both truncated arrays and single objects with trailing text
    items = []
    depth = 0
    obj_start = None
    in_str = False
    escape_next = False
    for i, ch in enumerate(raw):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_str:
            escape_next = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    items.append(json.loads(raw[obj_start : i + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None

    if len(items) == 1:
        return items[0]
    if items:
        return items

    return json.loads(raw)  # Re-raise original error for clean failure
