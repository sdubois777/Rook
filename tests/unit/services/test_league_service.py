"""Tests for LeagueService.get_league_config() fallback behavior."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.league_config import DEFAULT_LEAGUE_CONFIG


def _make_repo(league=None):
    repo = AsyncMock()
    repo.get_user_league = AsyncMock(return_value=league)
    return repo


def _make_user_league(*, team_count=10, draft_type="auction", scoring="half_ppr", budget=250):
    league = MagicMock()
    league.team_count = team_count
    league.draft_type = draft_type
    league.scoring = scoring
    league.budget = budget
    league.platform = "yahoo"
    league.league_id = "78512"
    league.season_year = 2026
    return league


@pytest.mark.asyncio
async def test_league_service_falls_back_to_default_on_none():
    """league_id=None returns DEFAULT_LEAGUE_CONFIG, no crash."""
    from backend.services.league_service import LeagueService

    repo = _make_repo()
    service = LeagueService(repo, AsyncMock())
    config = await service.get_league_config(uuid.uuid4(), None)

    assert config is DEFAULT_LEAGUE_CONFIG
    # Repo never called
    repo.get_user_league.assert_not_awaited()


@pytest.mark.asyncio
async def test_league_service_falls_back_to_default_on_miss():
    """Non-existent league returns DEFAULT_LEAGUE_CONFIG, no crash."""
    from backend.services.league_service import LeagueService

    repo = _make_repo(league=None)
    service = LeagueService(repo, AsyncMock())
    config = await service.get_league_config(uuid.uuid4(), uuid.uuid4())

    assert config is DEFAULT_LEAGUE_CONFIG


@pytest.mark.asyncio
async def test_league_service_returns_user_league_config():
    """Existing league returns config with user's actual settings."""
    from backend.services.league_service import LeagueService

    league = _make_user_league(team_count=10, budget=250, scoring="half_ppr")
    repo = _make_repo(league=league)
    service = LeagueService(repo, AsyncMock())
    config = await service.get_league_config(uuid.uuid4(), uuid.uuid4())

    assert config.team_count == 10
    assert config.budget == 250
    assert config.scoring == "half_ppr"
    assert config is not DEFAULT_LEAGUE_CONFIG
