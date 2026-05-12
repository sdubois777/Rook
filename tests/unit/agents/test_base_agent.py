"""
tests/unit/agents/test_base_agent.py

Tests for BaseAgent infrastructure:
- Exponential backoff on 529 Overloaded
- Exponential backoff on 429 RateLimitError
- Non-retryable errors re-raised immediately
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from backend.agents.base_agent import BaseAgent, HAIKU


# ---------------------------------------------------------------------------
# Minimal concrete subclass for testing
# ---------------------------------------------------------------------------

class _TestAgent(BaseAgent):
    AGENT_NAME = "test_agent"
    AGENT_MODEL = HAIKU
    AGENT_MAX_TOKENS = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_529_error():
    """Create a 529 APIStatusError like Anthropic SDK raises."""
    response = MagicMock()
    response.status_code = 529
    response.headers = {}
    return anthropic.APIStatusError(
        message="Overloaded",
        response=response,
        body={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    )


def _make_rate_limit_error():
    """Create a 429 RateLimitError."""
    response = MagicMock()
    response.status_code = 429
    response.headers = {}
    return anthropic.RateLimitError(
        message="Rate limited",
        response=response,
        body={"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited"}},
    )


def _make_success_response(text: str = "OK"):
    """Create a mock successful Message response."""
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    response.usage.input_tokens = 10
    response.usage.output_tokens = 5
    return response


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_529_triggers_backoff_not_immediate():
    """529 errors should trigger exponential backoff with asyncio.sleep, not immediate retries."""
    agent = _TestAgent(dry_run=False)

    # Fail twice with 529, then succeed
    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(
        side_effect=[
            _make_529_error(),
            _make_529_error(),
            _make_success_response("profile data"),
        ]
    )

    sleep_calls = []
    original_sleep = asyncio.sleep

    async def _mock_sleep(seconds):
        sleep_calls.append(seconds)

    with patch("backend.agents.base_agent.asyncio.sleep", side_effect=_mock_sleep):
        result = await agent._call_with_backoff(
            model=HAIKU,
            max_tokens=100,
            system="test",
            user="test",
        )

    assert result.content[0].text == "profile data"
    assert len(sleep_calls) == 2

    # Verify exponential increase: 2nd wait should be > 1st wait
    assert sleep_calls[1] > sleep_calls[0]

    # Base wait for 529 is 10s * 2^attempt
    # Attempt 0: ~10s + jitter(0-5), Attempt 1: ~20s + jitter(0-5)
    assert sleep_calls[0] >= 10  # 10 * 2^0
    assert sleep_calls[1] >= 20  # 10 * 2^1


@pytest.mark.asyncio
async def test_rate_limit_triggers_backoff():
    """429 RateLimitError should trigger backoff with shorter waits than 529."""
    agent = _TestAgent(dry_run=False)

    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(
        side_effect=[
            _make_rate_limit_error(),
            _make_success_response("ok"),
        ]
    )

    sleep_calls = []

    async def _mock_sleep(seconds):
        sleep_calls.append(seconds)

    with patch("backend.agents.base_agent.asyncio.sleep", side_effect=_mock_sleep):
        result = await agent._call_with_backoff(
            model=HAIKU,
            max_tokens=100,
            system="test",
            user="test",
        )

    assert result.content[0].text == "ok"
    assert len(sleep_calls) == 1

    # Base wait for 429 is 5s * 2^0 + jitter(0-2) = 5-7s
    assert sleep_calls[0] >= 5
    assert sleep_calls[0] < 10


@pytest.mark.asyncio
async def test_529_exhausts_retries_raises():
    """After max_retries 529 failures, the error should be re-raised."""
    agent = _TestAgent(dry_run=False)

    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(
        side_effect=[_make_529_error()] * 3
    )

    with patch("backend.agents.base_agent.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(anthropic.APIStatusError) as exc_info:
            await agent._call_with_backoff(
                model=HAIKU,
                max_tokens=100,
                system="test",
                user="test",
                max_retries=3,
            )
        assert exc_info.value.status_code == 529


@pytest.mark.asyncio
async def test_non_retryable_error_raised_immediately():
    """Non-529/429 errors should raise immediately without retries."""
    agent = _TestAgent(dry_run=False)

    response = MagicMock()
    response.status_code = 400
    response.headers = {}
    bad_request = anthropic.BadRequestError(
        message="Bad request",
        response=response,
        body={"type": "error", "error": {"type": "invalid_request_error"}},
    )

    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(side_effect=bad_request)

    sleep_calls = []

    async def _mock_sleep(seconds):
        sleep_calls.append(seconds)

    with patch("backend.agents.base_agent.asyncio.sleep", side_effect=_mock_sleep):
        with pytest.raises(anthropic.BadRequestError):
            await agent._call_with_backoff(
                model=HAIKU,
                max_tokens=100,
                system="test",
                user="test",
            )

    # No sleep should have been called — error is not retryable
    assert len(sleep_calls) == 0


@pytest.mark.asyncio
async def test_success_on_first_try_no_sleep():
    """Successful first call should not trigger any sleeps."""
    agent = _TestAgent(dry_run=False)

    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(
        return_value=_make_success_response("fast")
    )

    sleep_calls = []

    async def _mock_sleep(seconds):
        sleep_calls.append(seconds)

    with patch("backend.agents.base_agent.asyncio.sleep", side_effect=_mock_sleep):
        result = await agent._call_with_backoff(
            model=HAIKU,
            max_tokens=100,
            system="test",
            user="test",
        )

    assert result.content[0].text == "fast"
    assert len(sleep_calls) == 0
