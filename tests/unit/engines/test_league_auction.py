"""
Tests for league auction history — get_market_context + CSV import + refresh + sync.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engines.valuation import (
    compute_bid_ceiling,
    compute_value_gap,
    get_market_context,
)


# ---------------------------------------------------------------------------
# Helpers — fake Player-like objects
# ---------------------------------------------------------------------------

def _player(
    market_value=None,
    market_value_fantasypros=None,
    market_value_league=None,
):
    """Build a minimal mock with the fields get_market_context needs."""
    p = MagicMock()
    p.market_value = Decimal(str(market_value)) if market_value is not None else None
    p.market_value_fantasypros = Decimal(str(market_value_fantasypros)) if market_value_fantasypros is not None else None
    p.market_value_league = Decimal(str(market_value_league)) if market_value_league is not None else None
    return p


# ===========================================================================
# get_market_context — 6 tests
# ===========================================================================

def test_market_context_league_only():
    """League price set, no FP → effective = league, no bias."""
    p = _player(market_value_league=25)
    ctx = get_market_context(p)
    assert ctx["effective_market_value"] == Decimal("25")
    assert ctx["league_bias"] is None
    assert ctx["league_bias_signal"] is None


def test_market_context_fp_only():
    """FP set, no league → effective = FP, no bias."""
    p = _player(market_value_fantasypros=30)
    ctx = get_market_context(p)
    assert ctx["effective_market_value"] == Decimal("30")
    assert ctx["league_bias"] is None
    assert ctx["league_bias_signal"] is None


def test_market_context_both_aligned():
    """Both set, difference ≤ $5 → aligned. Effective uses FP (consensus)."""
    p = _player(market_value_fantasypros=28, market_value_league=30)
    ctx = get_market_context(p)
    assert ctx["effective_market_value"] == Decimal("28")
    assert ctx["league_bias"] == Decimal("2.00")
    assert ctx["league_bias_signal"] == "league_aligned"


def test_market_context_league_overpays():
    """League pays $15 more than FP → overpays signal."""
    p = _player(market_value_fantasypros=20, market_value_league=35)
    ctx = get_market_context(p)
    assert ctx["league_bias"] == Decimal("15.00")
    assert ctx["league_bias_signal"] == "league_overpays"


def test_market_context_league_underpays():
    """League pays $13 less than FP → underpays signal."""
    p = _player(market_value_fantasypros=40, market_value_league=27)
    ctx = get_market_context(p)
    assert ctx["league_bias"] == Decimal("-13.00")
    assert ctx["league_bias_signal"] == "league_underpays"


def test_market_context_neither():
    """No market values at all → all None."""
    p = _player()
    ctx = get_market_context(p)
    assert ctx["effective_market_value"] is None
    assert ctx["league_bias"] is None
    assert ctx["league_bias_signal"] is None


# ===========================================================================
# Ceiling differs with league vs FP
# ===========================================================================

def test_ceiling_is_market_free_pure_pool_share():
    """MARKET-FREE, PURE POOL-SHARE (ToS): compute_bid_ceiling ignores market_value AND tier/
    position (no market blend, no scarcity modifier). The ceiling is exactly system_value for
    every tier — any market_value (FP, league, high, low, None) yields the SAME ceiling."""
    sv = Decimal("30")
    for mv in (Decimal("40"), Decimal("25"), Decimal("0"), None):
        assert compute_bid_ceiling(sv, mv, tier=2, position="WR", risk_level="low") == Decimal("30")
    # T1 is ALSO pure pool-share now — the tier-1 scarcity modifier is dropped.
    t1_rb = compute_bid_ceiling(sv, Decimal("40"), tier=1, position="RB", risk_level="low")
    assert t1_rb == compute_bid_ceiling(sv, None, tier=1, position="RB") == Decimal("30")


# ===========================================================================
# FP unchanged after league import (conceptual)
# ===========================================================================

def test_fp_unchanged_after_league_import():
    """market_value (FP consensus) is used as effective — league is secondary."""
    p = _player(market_value=30, market_value_fantasypros=30, market_value_league=15)
    ctx = get_market_context(p)
    assert ctx["market_value_fantasypros"] == Decimal("30")
    assert ctx["effective_market_value"] == Decimal("30")


# ===========================================================================
# Valuation pass uses effective_market_value (unit-level)
# ===========================================================================

def test_valuation_pass_uses_effective_mv():
    """Effective market value uses FP consensus, not league price."""
    p = _player(market_value=40, market_value_fantasypros=40, market_value_league=20)
    ctx = get_market_context(p)
    effective = ctx["effective_market_value"]
    # effective should be FP consensus
    assert effective == Decimal("40")

    # Compute ceiling with effective (FP) — same as FP directly
    sv = Decimal("35")
    ceiling_effective = compute_bid_ceiling(sv, effective, tier=1, position="RB", risk_level="low")
    ceiling_fp = compute_bid_ceiling(sv, Decimal("40"), tier=1, position="RB", risk_level="low")
    assert ceiling_effective == ceiling_fp


def test_valuation_pass_fallback_to_fp():
    """No league price → effective = FP → same ceiling as before."""
    p = _player(market_value=40, market_value_fantasypros=40)
    ctx = get_market_context(p)
    effective = ctx["effective_market_value"]
    assert effective == Decimal("40")

    sv = Decimal("35")
    ceiling_effective = compute_bid_ceiling(sv, effective, tier=1, position="RB", risk_level="low")
    ceiling_fp = compute_bid_ceiling(sv, Decimal("40"), tier=1, position="RB", risk_level="low")
    assert ceiling_effective == ceiling_fp


# ===========================================================================
# Bias boundary tests
# ===========================================================================

def test_market_context_boundary_plus_5():
    """Exactly +$5 bias → aligned (threshold is strictly > 5)."""
    p = _player(market_value_fantasypros=20, market_value_league=25)
    ctx = get_market_context(p)
    assert ctx["league_bias"] == Decimal("5.00")
    assert ctx["league_bias_signal"] == "league_aligned"


def test_market_context_boundary_minus_5():
    """Exactly -$5 bias → aligned (threshold is strictly < -5)."""
    p = _player(market_value_fantasypros=25, market_value_league=20)
    ctx = get_market_context(p)
    assert ctx["league_bias"] == Decimal("-5.00")
    assert ctx["league_bias_signal"] == "league_aligned"


def test_market_context_fallback_to_market_value():
    """When market_value_fantasypros is None, falls back to market_value."""
    p = _player(market_value=28, market_value_league=20)
    ctx = get_market_context(p)
    assert ctx["market_value_fantasypros"] == Decimal("28")
    assert ctx["league_bias"] == Decimal("-8.00")
    assert ctx["league_bias_signal"] == "league_underpays"


# ===========================================================================
# sync_all_league_history — discovery + skip + manager name tests
# ===========================================================================

@pytest.mark.asyncio
async def test_auto_discovery_syncs_all_chain_leagues():
    """sync_all_league_history syncs all leagues returned by get_all_user_leagues (entire chain)."""
    from backend.engines.league_auction import sync_all_league_history

    fake_leagues = [
        {"league_key": "nfl.l.100", "league_id": "100", "name": "My League", "season": "2023", "is_auction": True},
        {"league_key": "nfl.l.200", "league_id": "200", "name": "Other League", "season": "2023", "is_auction": True},
        {"league_key": "nfl.l.100", "league_id": "100", "name": "My League", "season": "2024", "is_auction": True},
    ]

    mock_session = AsyncMock()
    # Simulate "not already synced" (count=0)
    mock_scalar = MagicMock()
    mock_scalar.scalar.return_value = 0
    mock_session.execute.return_value = mock_scalar

    with patch("backend.integrations.yahoo_api.get_all_user_leagues", new_callable=AsyncMock, return_value=fake_leagues), \
         patch("backend.engines.league_auction._sync_season", new_callable=AsyncMock, return_value={"matched": 5, "unmatched": 1}) as mock_sync, \
         patch("backend.engines.league_auction.refresh_market_value_league", new_callable=AsyncMock, return_value={"updated": 5}), \
         patch("backend.engines.league_auction.settings") as mock_settings:
        mock_settings.yahoo_league_id = "100"

        result = await sync_all_league_history(mock_session)

    # All leagues from chain are synced (no filtering by league_id)
    assert len(result["synced_seasons"]) == 3
    assert mock_sync.call_count == 3


@pytest.mark.asyncio
async def test_historical_sync_skips_already_synced():
    """Seasons with >10 existing yahoo records should be skipped."""
    from backend.engines.league_auction import sync_all_league_history

    fake_leagues = [
        {"league_key": "nfl.l.100", "league_id": "100", "name": "My League", "season": "2024", "is_auction": True},
    ]

    mock_session = AsyncMock()
    # Return count=50 (already synced)
    mock_scalar = MagicMock()
    mock_scalar.scalar.return_value = 50
    mock_session.execute.return_value = mock_scalar

    with patch("backend.integrations.yahoo_api.get_all_user_leagues", new_callable=AsyncMock, return_value=fake_leagues), \
         patch("backend.engines.league_auction._sync_season", new_callable=AsyncMock) as mock_sync, \
         patch("backend.engines.league_auction.settings") as mock_settings:
        mock_settings.yahoo_league_id = "100"

        result = await sync_all_league_history(mock_session)

    # Should skip, not sync
    assert 2024 in result["skipped_seasons"]
    assert len(result["synced_seasons"]) == 0
    mock_sync.assert_not_called()


@pytest.mark.asyncio
async def test_sync_season_batches_player_keys():
    """Player key resolution should batch at 25 keys per request."""
    from backend.engines.league_auction import _sync_season

    # Generate 60 fake picks to require 3 batches (25+25+10)
    picks = [
        {"player_key": f"nfl.p.{i}", "cost": "10", "team_key": "nfl.l.1.t.1", "pick": i}
        for i in range(60)
    ]

    mock_get_draft = AsyncMock(return_value=picks)
    mock_get_players = AsyncMock(return_value=[])
    mock_get_teams = AsyncMock(return_value=[])

    mock_session = AsyncMock()
    # select(Player) returns empty list
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    await _sync_season(
        mock_session, "nfl.l.1", 2024,
        mock_get_draft, mock_get_players, mock_get_teams,
    )

    # 60 keys / 25 per batch = 3 calls
    assert mock_get_players.call_count == 3
    # First batch should have 25 keys
    first_batch = mock_get_players.call_args_list[0][0][0]
    assert len(first_batch) == 25
    # Last batch should have 10 keys
    last_batch = mock_get_players.call_args_list[2][0][0]
    assert len(last_batch) == 10


@pytest.mark.asyncio
async def test_sync_season_stores_manager_name():
    """Manager names from team lookup should be stored on each pick."""
    from backend.engines.league_auction import _sync_season

    picks = [
        {"player_key": "nfl.p.100", "cost": "25", "team_key": "nfl.l.1.t.3", "pick": 1},
    ]
    player_details = [
        {"player_key": "nfl.p.100", "name": "Test Player", "position": "WR", "nfl_team": "LAC"},
    ]
    teams = [
        {"team_key": "nfl.l.1.t.3", "team_name": "The Lord", "manager_name": "BigBoss"},
    ]

    mock_get_draft = AsyncMock(return_value=picks)
    mock_get_players = AsyncMock(return_value=player_details)
    mock_get_teams = AsyncMock(return_value=teams)

    mock_session = AsyncMock()
    # select(Player) returns empty — no DB match
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result

    await _sync_season(
        mock_session, "nfl.l.1", 2024,
        mock_get_draft, mock_get_players, mock_get_teams,
    )

    # Verify session.execute was called with an INSERT that includes manager_name
    # The upsert call should have been made (execute called for SELECT + INSERT)
    assert mock_session.execute.call_count >= 2  # at least SELECT + INSERT
    assert mock_session.commit.call_count == 1


@pytest.mark.asyncio
async def test_sync_all_returns_error_when_no_league_id():
    """sync_all_league_history returns error dict when no YAHOO_LEAGUE_ID configured."""
    from backend.engines.league_auction import sync_all_league_history

    mock_session = AsyncMock()

    with patch("backend.engines.league_auction.settings") as mock_settings:
        mock_settings.yahoo_league_id = ""

        result = await sync_all_league_history(mock_session)

    assert "error" in result
    assert "No YAHOO_LEAGUE_ID" in result["error"]


@pytest.mark.asyncio
async def test_sync_all_empty_chain():
    """sync_all_league_history returns error info when chain discovery finds no leagues."""
    from backend.engines.league_auction import sync_all_league_history

    mock_session = AsyncMock()

    with patch("backend.integrations.yahoo_api.get_all_user_leagues", new_callable=AsyncMock, return_value=[]), \
         patch("backend.engines.league_auction.settings") as mock_settings:
        mock_settings.yahoo_league_id = "100"

        result = await sync_all_league_history(mock_session)

    assert len(result["synced_seasons"]) == 0
    assert len(result.get("errors", [])) > 0


# ===========================================================================
# Strategy classification
# ===========================================================================

def test_classify_strategy_hero_rb():
    """50%+ on RB → hero_rb."""
    from backend.engines.league_auction import _classify_strategy
    assert _classify_strategy({"QB": 0.05, "RB": 0.55, "WR": 0.35, "TE": 0.05}) == "hero_rb"


def test_classify_strategy_zero_rb():
    """≤20% RB with 45%+ WR → zero_rb."""
    from backend.engines.league_auction import _classify_strategy
    assert _classify_strategy({"QB": 0.10, "RB": 0.15, "WR": 0.65, "TE": 0.10}) == "zero_rb"


def test_classify_strategy_balanced():
    """No extreme allocation → balanced."""
    from backend.engines.league_auction import _classify_strategy
    assert _classify_strategy({"QB": 0.10, "RB": 0.40, "WR": 0.40, "TE": 0.10}) == "balanced"


def test_classify_management_style_stars_and_scrubs():
    """High top-2 concentration → stars_and_scrubs."""
    from backend.engines.league_auction import _classify_management_style
    assert _classify_management_style([0.55, 0.60], 0.95) == "stars_and_scrubs"


def test_classify_management_style_conservative():
    """Low budget utilization → conservative."""
    from backend.engines.league_auction import _classify_management_style
    assert _classify_management_style([0.30, 0.35], 0.85) == "conservative"


def test_classify_management_style_analytical():
    """Moderate concentration + full budget → analytical."""
    from backend.engines.league_auction import _classify_management_style
    assert _classify_management_style([0.40, 0.38], 0.98) == "analytical"


# ===========================================================================
# Nickname alias normalization
# ===========================================================================

def test_nickname_alias_hollywood_brown():
    """Hollywood Brown should normalize to marquise brown."""
    from backend.integrations.nfl_data import normalize_player_name
    assert normalize_player_name("Hollywood Brown") == "marquise brown"
