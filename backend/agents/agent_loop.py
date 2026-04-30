"""
Iterative agentic loop — tool-use round-trips with the Anthropic API.

Only used by the live draft agent (backend/engines/live_draft.py).
Pre-draft pipeline agents use BaseAgent.call_once() instead.

If you are importing run_agent() in any file other than live_draft.py,
stop and reconsider — you almost certainly want call_once() instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

import anthropic
from anthropic.types import Message, ToolUseBlock, TextBlock

from backend.agents.base_agent import get_client

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict], Awaitable[Any]]
ToolDefinition = dict  # Anthropic tool schema dict


async def run_agent(
    system_prompt: str,
    user_message: str,
    tools: list[ToolDefinition],
    tool_handler: ToolHandler,
    model: str,
    max_tokens: int,
    max_iterations: int = 20,
    temperature: float = 0.2,
) -> str:
    """
    Run a full agentic loop until the model stops using tools.

    model and max_tokens are REQUIRED — no defaults.
    Returns the final text response from the model.

    Only use this for the live draft agent. Pipeline agents use call_once().
    """
    client = get_client()
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for iteration in range(max_iterations):
        response = await _call_with_retry(
            client.messages.create,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        # If model is done with tools, return the final text
        if response.stop_reason == "end_turn":
            return _extract_text(response)

        # Process tool calls
        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    logger.debug("Tool call: %s(%s)", block.name, json.dumps(block.input)[:200])
                    try:
                        result = await tool_handler(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": _serialize_result(result),
                        })
                    except Exception as exc:
                        logger.warning("Tool %s failed: %s", block.name, exc)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {exc}",
                            "is_error": True,
                        })

            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning("Unexpected stop_reason: %s", response.stop_reason)
            return _extract_text(response)

    logger.warning("Agent hit max_iterations=%d without finishing", max_iterations)
    return _extract_text(response)


async def _call_with_retry(fn, *args, max_retries: int = 3, **kwargs) -> Message:
    """Exponential backoff on rate limits and transient errors."""
    for attempt in range(max_retries):
        try:
            return await fn(*args, **kwargs)
        except anthropic.RateLimitError:
            wait = 2 ** attempt * 5  # 5s, 10s, 20s
            logger.warning("Rate limited — waiting %ds (attempt %d/%d)", wait, attempt + 1, max_retries)
            await asyncio.sleep(wait)
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 and attempt < max_retries - 1:
                wait = 2 ** attempt * 2
                logger.warning("Server error %d — retrying in %ds", exc.status_code, wait)
                await asyncio.sleep(wait)
            else:
                raise
    raise RuntimeError(f"Agent call failed after {max_retries} retries")


def _extract_text(response: Message) -> str:
    parts = [block.text for block in response.content if isinstance(block, TextBlock)]
    return "\n".join(parts).strip()


def _serialize_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


# ---------------------------------------------------------------------------
# Tool definition helpers (used by live_draft.py tool schemas)
# ---------------------------------------------------------------------------

def tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> ToolDefinition:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


def string_prop(description: str) -> dict:
    return {"type": "string", "description": description}


def number_prop(description: str) -> dict:
    return {"type": "number", "description": description}


def bool_prop(description: str) -> dict:
    return {"type": "boolean", "description": description}


def array_prop(description: str, item_type: str = "string") -> dict:
    return {"type": "array", "items": {"type": item_type}, "description": description}
