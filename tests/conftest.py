"""
Standard test fixtures — define once, use everywhere.

All fixtures here mock external dependencies so unit tests:
  - Never call the real Anthropic API
  - Never connect to a real database
  - Never load real nfl_data_py data
  - Never launch a real browser

See docs/rules/GIT_RULES.md for fixture specifications.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Anthropic API mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_anthropic():
    """
    Mocks the Anthropic AsyncAnthropic client.
    Returns a client whose messages.create() returns a minimal valid response.
    Patch target covers all agent imports of the client.
    """
    with patch("backend.agents.base_agent.get_client") as mock_get_client:
        client = AsyncMock()
        mock_get_client.return_value = client
        client.messages.create.return_value = MagicMock(
            content=[MagicMock(text='{"result": "mocked"}')],
            usage=MagicMock(input_tokens=100, output_tokens=50),
            stop_reason="end_turn",
        )
        yield client


# ---------------------------------------------------------------------------
# Database mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    """
    Mocks AsyncSessionLocal so no real DB connections are made.
    Returns a session mock that can be further configured per test.
    """
    with patch("backend.database.AsyncSessionLocal") as mock_session_factory:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = session
        yield session


# ---------------------------------------------------------------------------
# NFL data mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_nfl_data():
    """
    Mocks nfl_data_py so no real data is downloaded.
    Returns a MagicMock that can be configured per test.
    """
    with patch("backend.integrations.nfl_data") as mock:
        yield MagicMock()


# ---------------------------------------------------------------------------
# Playwright mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_playwright():
    """
    Mocks Playwright so no real browser is launched.
    """
    with patch("playwright.async_api.async_playwright") as mock:
        yield AsyncMock()


# ---------------------------------------------------------------------------
# Fixture data loaders
# ---------------------------------------------------------------------------

@pytest.fixture
def team_systems_fixtures() -> list[dict]:
    with open(FIXTURES_DIR / "team_systems.json") as f:
        return json.load(f)


@pytest.fixture
def players_fixtures() -> list[dict]:
    with open(FIXTURES_DIR / "players.json") as f:
        return json.load(f)


@pytest.fixture
def draft_state_fixture() -> dict:
    with open(FIXTURES_DIR / "draft_state.json") as f:
        return json.load(f)
