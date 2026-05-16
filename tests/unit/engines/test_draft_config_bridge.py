"""Tests for DraftStateManager.config_from_user_league() bridge."""
from __future__ import annotations

from unittest.mock import MagicMock

from backend.engines.draft_state_manager import DraftStateManager, LeagueConfig


def _make_user_league(*, budget=250, team_count=10, draft_type="auction"):
    league = MagicMock()
    league.budget = budget
    league.team_count = team_count
    league.draft_type = draft_type
    return league


def test_draft_config_from_user_league_uses_budget():
    league = _make_user_league(budget=250)
    config = DraftStateManager.config_from_user_league(league)
    assert config.auction_budget == 250


def test_draft_config_from_user_league_uses_team_count():
    league = _make_user_league(team_count=10)
    config = DraftStateManager.config_from_user_league(league)
    assert config.team_count == 10


def test_draft_config_fallback_on_none_league():
    config = DraftStateManager.config_from_user_league(None)
    assert config.auction_budget == 200
    assert config.team_count == 12
    assert config.min_bid == 1


def test_draft_config_snake_league_zero_budget():
    """Snake draft leagues get auction_budget=0."""
    league = _make_user_league(draft_type="snake", budget=200)
    config = DraftStateManager.config_from_user_league(league)
    assert config.auction_budget == 0


def test_draft_config_default_roster_slots():
    """Roster slots remain default regardless of user league."""
    league = _make_user_league()
    config = DraftStateManager.config_from_user_league(league)
    assert config.roster_slots["QB"] == 1
    assert config.roster_slots["RB"] == 2
    assert config.roster_slots["BENCH"] == 7


def test_league_config_team_count_field():
    """LeagueConfig dataclass has team_count field with default 12."""
    config = LeagueConfig()
    assert config.team_count == 12

    config2 = LeagueConfig(team_count=14)
    assert config2.team_count == 14
