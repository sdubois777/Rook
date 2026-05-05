"""
Tests for backend/routers/assistant.py — AI Assistant chat endpoint.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.routers.assistant import (
    _format_player_context,
    build_assistant_context,
)


# ---------------------------------------------------------------------------
# _format_player_context
# ---------------------------------------------------------------------------

def _mock_player(
    name="Saquon Barkley",
    position="RB",
    team_abbr="PHI",
    tier=1,
    bid_ceiling=68,
    baseline_value=71,
    market_value=65,
    value_gap=6,
    value_gap_signal="market_undervalues",
):
    player = MagicMock()
    player.name = name
    player.position = position
    player.team_abbr = team_abbr
    player.tier = tier
    player.recommended_bid_ceiling = Decimal(str(bid_ceiling))
    player.baseline_value = Decimal(str(baseline_value))
    player.market_value = Decimal(str(market_value))
    player.value_gap = Decimal(str(value_gap))
    player.value_gap_signal = value_gap_signal
    player.situation_score = "strong"
    player.let_go_threshold = Decimal("78.20")
    player.notes = "Elite three-down back"

    # Profile
    profile = MagicMock()
    profile.role_classification = "bellcow"
    profile.career_trajectory = "peak"
    profile.clean_season_baseline = {"ppr_points": 310.5, "receptions": 85, "yards": 1800, "tds": 12}
    player.profile = profile

    # Injury
    inj = MagicMock()
    inj.overall_risk_level = "moderate"
    inj.post_acl_flag = False
    inj.workload_cliff_flag = True
    inj.risk_adjusted_value_modifier = Decimal("-0.10")
    player.injury_profile = inj

    # Schedule
    sched = MagicMock()
    sched.early_window_grade = "favorable"
    sched.full_season_grade = "neutral"
    sched.playoff_window_grade = "favorable"
    sched.bye_in_playoff_window = False
    player.schedule = sched

    # Dependencies
    dep = MagicMock()
    dep.flag_type = "workload_cliff"
    dep.trigger_player_name = None
    dep.reasoning = "High career touch count, age 28"
    player.dependencies = [dep]

    return player


def test_format_player_context_includes_name_and_position():
    player = _mock_player()
    result = _format_player_context(player)
    assert "PLAYER: Saquon Barkley (RB, PHI)" in result


def test_format_player_context_includes_valuation():
    player = _mock_player()
    result = _format_player_context(player)
    assert "Tier: 1" in result
    assert "Bid ceiling: $68" in result
    assert "System value: $71" in result
    assert "Market value: $65" in result


def test_format_player_context_includes_injury():
    player = _mock_player()
    result = _format_player_context(player)
    assert "Injury risk: moderate" in result
    assert "WORKLOAD_CLIFF" in result


def test_format_player_context_includes_schedule():
    player = _mock_player()
    result = _format_player_context(player)
    assert "Playoffs: favorable" in result


def test_format_player_context_includes_flags():
    player = _mock_player()
    result = _format_player_context(player)
    assert "WORKLOAD_CLIFF" in result
    assert "High career touch count" in result


def test_format_player_context_includes_notes():
    player = _mock_player()
    result = _format_player_context(player)
    assert "Elite three-down back" in result


def test_format_player_context_handles_free_agent():
    player = _mock_player(team_abbr=None, tier=None)
    player.tier = None
    player.recommended_bid_ceiling = None
    player.baseline_value = None
    player.market_value = None
    player.value_gap = None
    player.value_gap_signal = None
    result = _format_player_context(player)
    assert "FA" in result


def test_format_player_context_handles_no_profile():
    player = _mock_player()
    player.profile = None
    player.injury_profile = None
    player.schedule = None
    player.dependencies = []
    player.notes = None
    result = _format_player_context(player)
    assert "Saquon Barkley" in result


# ---------------------------------------------------------------------------
# build_assistant_context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_context_includes_league_context():
    """Context always includes league settings."""
    with patch("backend.routers.assistant.AsyncSessionLocal") as mock_session_cls:
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        # Return empty results for all queries
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        mock_session_cls.return_value = session

        result = await build_assistant_context(
            message="test",
            context_type="general",
            player_ids=[],
            include_roster=False,
            include_opponents=False,
        )

    assert "12-team PPR" in result
    assert "$185" in result
    assert "RB=$80" in result


@pytest.mark.asyncio
async def test_build_context_includes_explicit_players():
    """When player_ids are provided, those players are included in context."""
    import uuid

    player_id = uuid.uuid4()
    mock_player = _mock_player()
    mock_player.id = player_id

    with patch("backend.routers.assistant.AsyncSessionLocal") as mock_session_cls:
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        # First call: explicit player lookup, second: mentioned players, third: value gaps, fourth: signals
        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:
                # Return the player for explicit lookup
                result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[mock_player])))
            else:
                result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
            return result

        session.execute = mock_execute
        mock_session_cls.return_value = session

        result = await build_assistant_context(
            message="test",
            context_type="general",
            player_ids=[str(player_id)],
            include_roster=False,
            include_opponents=False,
        )

    assert "Saquon Barkley" in result
