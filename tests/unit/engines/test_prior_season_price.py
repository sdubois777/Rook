"""
Tests for market_value_historic — snapshot, API exposure, valuation agent context.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engines.market_values import sync_market_values, _snapshot_current_market_values


# ---------------------------------------------------------------------------
# Snapshot — preserves current FP values before overwrite
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_runs_before_overwrite():
    """sync_market_values calls _snapshot_current_market_values before scraping."""
    fake_player = MagicMock()
    fake_player.name = "Patrick Mahomes"
    fake_player.market_value = Decimal("35")
    fake_player.market_value_fantasypros = Decimal("35")
    fake_player.market_value_prior_season = None
    fake_player.market_value_prior_season_year = None
    fake_player.market_value_confidence = "medium"
    fake_player.market_value_updated_at = None

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [fake_player]
    # execute returns different things for snapshot vs player load
    mock_result.all.return_value = []  # snapshot query returns no rows
    mock_session.execute.return_value = mock_result

    scraped = [{"name": "Patrick Mahomes", "avg_value": 40.0, "min_value": 35, "max_value": 45}]

    with patch(
        "backend.engines.market_values._scrape_in_thread",
        return_value=(scraped, 2026, True),
    ), patch(
        "backend.engines.market_values._store_metadata",
        new_callable=AsyncMock,
    ), patch(
        "backend.engines.market_values._snapshot_current_market_values",
        new_callable=AsyncMock,
        return_value=0,
    ) as mock_snapshot:
        result = await sync_market_values(mock_session)

    # Snapshot was called
    mock_snapshot.assert_awaited_once_with(mock_session)
    assert result["matched"] == 1


@pytest.mark.asyncio
async def test_snapshot_skipped_on_dry_run():
    """Dry run does not snapshot."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.all.return_value = []
    mock_session.execute.return_value = mock_result

    with patch(
        "backend.engines.market_values._scrape_in_thread",
        return_value=([], 2026, True),
    ), patch(
        "backend.engines.market_values._snapshot_current_market_values",
        new_callable=AsyncMock,
    ) as mock_snapshot:
        await sync_market_values(mock_session, dry_run=True)

    mock_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_is_idempotent():
    """Running snapshot twice same year does not duplicate rows (ON CONFLICT DO NOTHING)."""
    mock_session = AsyncMock()

    # Simulate player rows
    player_row = MagicMock()
    player_row.id = "fake-uuid"
    player_row.market_value_fantasypros = Decimal("40")

    mock_result = MagicMock()
    mock_result.all.return_value = [player_row]
    mock_session.execute.return_value = mock_result

    with patch("backend.engines.market_values.get_current_season", return_value=2026):
        count = await _snapshot_current_market_values(mock_session)

    assert count == 1
    # execute called twice: SELECT + INSERT
    assert mock_session.execute.call_count == 2
    mock_session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_returns_zero_when_no_players():
    """Snapshot returns 0 when no players have FP values."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.execute.return_value = mock_result

    with patch("backend.engines.market_values.get_current_season", return_value=2026):
        count = await _snapshot_current_market_values(mock_session)

    assert count == 0
    # Only SELECT, no INSERT
    assert mock_session.execute.call_count == 1


# ---------------------------------------------------------------------------
# Rotation — existing FP value moves to prior_season on refresh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotation_on_refresh():
    """When FP value changes, old value rotates to prior_season on players table."""
    fake_player = MagicMock()
    fake_player.name = "Patrick Mahomes"
    fake_player.market_value = Decimal("35")
    fake_player.market_value_fantasypros = Decimal("35")
    fake_player.market_value_prior_season = None
    fake_player.market_value_prior_season_year = None
    fake_player.market_value_confidence = "medium"
    fake_player.market_value_updated_at = None

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [fake_player]
    mock_session.execute.return_value = mock_result

    scraped = [{"name": "Patrick Mahomes", "avg_value": 40.0, "min_value": 35, "max_value": 45}]

    with patch(
        "backend.engines.market_values._scrape_in_thread",
        return_value=(scraped, 2026, True),
    ), patch(
        "backend.engines.market_values._store_metadata",
        new_callable=AsyncMock,
    ), patch(
        "backend.engines.market_values._snapshot_current_market_values",
        new_callable=AsyncMock,
        return_value=0,
    ):
        result = await sync_market_values(mock_session)

    assert result["matched"] == 1
    assert fake_player.market_value_prior_season == Decimal("35")
    assert fake_player.market_value_prior_season_year == 2025
    assert fake_player.market_value_fantasypros == 40.0


# ---------------------------------------------------------------------------
# Valuation agent context is MARKET-BLIND (ToS): market_value_fantasypros and
# prior_season_price are stripped from _build_player_context on every path — even
# when they are available — so the blind price opinion never sees market. Market
# re-enters only in the deterministic post-pass (reconcile_value_signals).
# ---------------------------------------------------------------------------

def test_valuation_agent_context_excludes_market_even_when_available():
    """_build_player_context must NOT include market_value_fantasypros or
    prior_season_price, even for a player that HAS both."""
    from backend.agents.valuation_agent import ValuationAgent

    agent = ValuationAgent.__new__(ValuationAgent)

    # Mock historic price record (available — must still be excluded)
    hist = MagicMock()
    hist.season_year = 2025
    hist.price = Decimal("42")

    player = MagicMock()
    player.name = "CeeDee Lamb"
    player.position = "WR"
    player.team_abbr = "DAL"
    player.age = 26
    player.tier = 1
    player.is_rookie = False
    player.recommended_bid_ceiling = Decimal("55")
    player.baseline_value = Decimal("50")
    player.market_value = Decimal("48")
    player.value_gap = Decimal("2")
    player.value_gap_signal = "aligned"
    player.ceiling_value = Decimal("60")
    player.floor_value = Decimal("35")
    player.market_value_fantasypros = Decimal("48")
    player.historic_prices = [hist]
    player.profile = None
    player.injury_profile = None
    player.schedule = None
    player.dependencies = []

    with patch("backend.agents.valuation_agent.get_current_season", return_value=2026):
        ctx = agent._build_player_context(player)

    for k in ("market_value", "value_gap", "value_gap_signal",
              "market_value_fantasypros", "prior_season_price"):
        assert k not in ctx, f"{k} must be stripped from the blind PPR context"
    # The non-market math anchor is still present.
    assert ctx["math_bid_ceiling"] == 55.0


def test_valuation_prompt_no_league_language():
    """System prompt must not use 'your league' except in NEVER/forbidden instructions."""
    from backend.agents.valuation_agent import SYSTEM_PROMPT

    forbidden = ["your league paid", "your league values", "in your league"]
    for line in SYSTEM_PROMPT.splitlines():
        stripped = line.strip().lower()
        # Skip lines that are instructions about what NOT to say
        if "never" in stripped or "forbidden" in stripped or "correct" in stripped:
            continue
        for phrase in forbidden:
            assert phrase not in stripped, (
                f"Found forbidden phrase '{phrase}' in non-instruction line: {line.strip()}"
            )
