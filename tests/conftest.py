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
# PROD-DB KILL SWITCH — the class-level fix, not a per-test patch.
# ---------------------------------------------------------------------------
# The old `mock_db` fixture was OPT-IN and patched backend.database.AsyncSessionLocal
# by attribute — so the ~25 modules that hold their own imported reference bypassed it,
# and any test requesting no fixtures had NO protection at all. That is how a test run
# deleted 129 prod dependency rows. These two hooks refuse to run the ENTIRE suite when
# DATABASE_URL points at a prod host, REGARDLESS of what any individual test or module
# does with sessions — a new code path that opens its own session cannot reopen the hole.
#
# Keyed on the DB HOST (via backend.db_guard), NEVER on settings.environment (which has
# been observed reading "development" against a prod DB).

def _refuse_if_prod(where: str) -> None:
    from backend.db_guard import is_prod_db, db_host
    if is_prod_db():
        pytest.exit(
            "\n" + "=" * 72 + "\n"
            "  [!!] TEST SUITE REFUSING TO RUN -- DATABASE_URL POINTS AT PRODUCTION\n"
            + "=" * 72 + "\n"
            f"  DB host: {db_host()}   (detected via {where})\n"
            "  Tests open real sessions and WILL mutate whatever they point at.\n"
            "  Point DATABASE_URL at your dev DB (localhost:5433) and re-run.\n"
            "  There is intentionally NO override -- tests never run against prod.\n"
            + "=" * 72,
            returncode=2,
        )


def pytest_configure(config):
    """Fail before collection/import — the earliest possible point."""
    _refuse_if_prod("pytest_configure")


@pytest.fixture(scope="session", autouse=True)
def _forbid_prod_db():
    """Session-scoped, autouse: a second gate that fires before the first test even if
    pytest_configure is somehow bypassed. Autouse ⇒ a fixture-less test is still guarded."""
    _refuse_if_prod("session fixture")
    yield


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
