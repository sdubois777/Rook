"""
Tests for backend/engines/valuation.py — Stage 9: Draft Bible Valuation Pass

All pure functions are tested directly (no mocks needed).
The async run_valuation_pass() test mocks AsyncSessionLocal.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engines.valuation import (
    ANCHOR_WEIGHTS,
    SCARCITY_MODIFIERS,
    VALUE_GAP_OVERVALUE_THRESHOLD,
    VALUE_GAP_UNDERVALUE_THRESHOLD,
    assign_tier,
    compute_bid_ceiling,
    compute_let_go_threshold,
    compute_value_gap,
    ppr_to_system_value,
    run_valuation_pass,
)


# ===========================================================================
# assign_tier
# ===========================================================================

def test_assign_tier_top3_is_tier1():
    assert assign_tier(1) == 1
    assert assign_tier(3) == 1


def test_assign_tier_rank4_to_9_is_tier2():
    assert assign_tier(4) == 2
    assert assign_tier(9) == 2


def test_assign_tier_rank10_to_19_is_tier3():
    assert assign_tier(10) == 3
    assert assign_tier(19) == 3


def test_assign_tier_rank20_to_34_is_tier4():
    assert assign_tier(20) == 4
    assert assign_tier(34) == 4


def test_assign_tier_rank35_plus_is_tier5():
    assert assign_tier(35) == 5
    assert assign_tier(200) == 5


# ===========================================================================
# ppr_to_system_value
# ===========================================================================

def test_ppr_to_system_value_returns_proportional_dollar():
    # 200 PPR, replacement at 100 PPR → 100 PAR out of 300 total PAR, budget $500
    val = ppr_to_system_value(200, 100, 300, 500)
    assert val == Decimal("166.67")


def test_ppr_to_system_value_at_replacement_returns_one_dollar():
    val = ppr_to_system_value(100, 100, 300, 500)
    assert val == Decimal("1.00")


def test_ppr_to_system_value_below_replacement_returns_one_dollar():
    val = ppr_to_system_value(80, 100, 300, 500)
    assert val == Decimal("1.00")


def test_ppr_to_system_value_zero_total_par_returns_one_dollar():
    val = ppr_to_system_value(200, 200, 0, 500)
    assert val == Decimal("1.00")


# ===========================================================================
# compute_bid_ceiling — Tier 1
# ===========================================================================

def test_tier1_bid_ceiling_uses_anchor_weight():
    """Tier 1: blend = system × 0.20 + market × 0.80, then × scarcity × risk_factor."""
    sv = Decimal("40")
    mv = Decimal("50")
    # anchor = 0.80 → blend = 40×0.20 + 50×0.80 = 8 + 40 = 48
    # scarcity for WR T1 = 1.20 → 48 × 1.20 = 57.60
    # risk factor = 1 + 0 = 1.0 → ceiling = 57.60
    ceiling = compute_bid_ceiling(sv, mv, tier=1, position="WR", risk_modifier=Decimal("0"))
    assert ceiling == Decimal("57.60")


def test_tier1_anchor_weight_is_0_80():
    assert ANCHOR_WEIGHTS[1] == Decimal("0.80")


def test_scarcity_modifier_applied_to_tier1_rb():
    """Tier 1 RB scarcity modifier = 1.35."""
    sv = Decimal("40")
    mv = Decimal("40")
    # blend = 40×0.20 + 40×0.80 = 40, scarcity 1.35 → 40×1.35 = 54
    ceiling = compute_bid_ceiling(sv, mv, tier=1, position="RB", risk_modifier=None)
    assert ceiling == Decimal("54.00")


def test_tier1_rb_scarcity_modifier_is_1_35():
    assert SCARCITY_MODIFIERS["RB"] == Decimal("1.35")


def test_tier1_wr_scarcity_modifier_is_1_20():
    assert SCARCITY_MODIFIERS["WR"] == Decimal("1.20")


# ===========================================================================
# compute_bid_ceiling — Tier 2/3
# ===========================================================================

def test_tier2_bid_ceiling_uses_85_15_blend():
    """Tier 2: blend = system × 0.85 + market × 0.15."""
    sv = Decimal("20")
    mv = Decimal("30")
    # blend = 20×0.85 + 30×0.15 = 17 + 4.5 = 21.5
    ceiling = compute_bid_ceiling(sv, mv, tier=2, position="WR", risk_modifier=None)
    assert ceiling == Decimal("21.50")


def test_tier3_bid_ceiling_uses_85_15_blend():
    sv = Decimal("10")
    mv = Decimal("15")
    # blend = 10×0.85 + 15×0.15 = 8.5 + 2.25 = 10.75
    ceiling = compute_bid_ceiling(sv, mv, tier=3, position="RB", risk_modifier=None)
    assert ceiling == Decimal("10.75")


# ===========================================================================
# compute_bid_ceiling — Tier 4/5
# ===========================================================================

def test_tier4_bid_ceiling_ignores_market_value():
    """Tier 4: ceiling = system_value × (1 + risk_modifier), market ignored."""
    sv = Decimal("5")
    mv = Decimal("100")   # large market value — should not influence ceiling
    ceiling = compute_bid_ceiling(sv, mv, tier=4, position="RB", risk_modifier=None)
    assert ceiling == Decimal("5.00")


def test_tier5_bid_ceiling_ignores_market_value():
    sv = Decimal("3")
    mv = Decimal("50")
    ceiling = compute_bid_ceiling(sv, mv, tier=5, position="WR", risk_modifier=None)
    assert ceiling == Decimal("3.00")


def test_tier4_bid_ceiling_uses_system_value_only():
    """Confirm Tier 4-5 is exactly system_value when no risk modifier."""
    sv = Decimal("7")
    ceiling = compute_bid_ceiling(sv, None, tier=4, position="QB", risk_modifier=None)
    assert ceiling == sv


# ===========================================================================
# compute_bid_ceiling — risk modifier
# ===========================================================================

def test_risk_modifier_reduces_ceiling_correctly():
    """A negative risk modifier (e.g. -0.20) reduces ceiling by 20%."""
    sv = Decimal("30")
    # Tier 2, no market value → blend = 30×0.85 + 30×0.15 = 30, then × 0.80
    ceiling = compute_bid_ceiling(
        sv, None, tier=2, position="WR", risk_modifier=Decimal("-0.20")
    )
    assert ceiling == Decimal("24.00")


def test_zero_risk_modifier_no_change():
    sv = Decimal("20")
    ceiling_no_rm = compute_bid_ceiling(sv, None, tier=3, position="TE", risk_modifier=None)
    ceiling_zero  = compute_bid_ceiling(sv, None, tier=3, position="TE", risk_modifier=Decimal("0"))
    assert ceiling_no_rm == ceiling_zero


def test_ceiling_minimum_is_one_dollar():
    """Even with a large negative risk modifier, ceiling never drops below $1."""
    sv = Decimal("1")
    ceiling = compute_bid_ceiling(
        sv, None, tier=5, position="RB", risk_modifier=Decimal("-0.99")
    )
    assert ceiling == Decimal("1.00")


def test_market_value_none_uses_system_value_for_blend():
    """When market_value is None, market treated as = system_value → neutral blend."""
    sv = Decimal("20")
    # Tier 2, no market → blend = 20×0.85 + 20×0.15 = 20
    ceiling = compute_bid_ceiling(sv, None, tier=2, position="WR", risk_modifier=None)
    assert ceiling == Decimal("20.00")


# ===========================================================================
# compute_value_gap
# ===========================================================================

def test_value_gap_signal_market_undervalues():
    """gap > 5 → market_undervalues (our system sees more value)."""
    gap, signal = compute_value_gap(Decimal("30"), Decimal("20"))
    assert gap == Decimal("10.00")
    assert signal == "market_undervalues"


def test_value_gap_signal_market_overvalues():
    """gap < -5 → market_overvalues (room will pay more than player is worth)."""
    gap, signal = compute_value_gap(Decimal("20"), Decimal("30"))
    assert gap == Decimal("-10.00")
    assert signal == "market_overvalues"


def test_value_gap_signal_aligned():
    """gap within ±5 → aligned."""
    gap, signal = compute_value_gap(Decimal("20"), Decimal("22"))
    assert gap == Decimal("-2.00")
    assert signal == "aligned"


def test_value_gap_none_market_returns_none():
    gap, signal = compute_value_gap(Decimal("30"), None)
    assert gap is None
    assert signal is None


def test_value_gap_exact_threshold_aligned():
    """Gap of exactly ±5 should be 'aligned' (boundary not a trigger)."""
    _, signal_over  = compute_value_gap(Decimal("20"), Decimal("25"))
    _, signal_under = compute_value_gap(Decimal("25"), Decimal("20"))
    # gap = -5 → not < -5 → aligned; gap = 5 → not > 5 → aligned
    assert signal_over  == "aligned"
    assert signal_under == "aligned"


# ===========================================================================
# compute_let_go_threshold
# ===========================================================================

def test_let_go_threshold_is_ceiling_plus_15_pct():
    ceiling = Decimal("40")
    let_go = compute_let_go_threshold(ceiling)
    assert let_go == Decimal("46.00")


def test_let_go_threshold_rounds_to_two_dp():
    ceiling = Decimal("33")
    let_go = compute_let_go_threshold(ceiling)
    assert let_go == Decimal("37.95")


# ===========================================================================
# run_valuation_pass — async integration (DB mocked)
# ===========================================================================

def _make_player(
    position: str,
    ppr_points: float,
    market_value: float | None = None,
    risk_modifier: float | None = None,
) -> MagicMock:
    """Helper: create a mock Player with nested profile + injury_profile."""
    player = MagicMock(spec=["position", "profile", "injury_profile", "market_value",
                              "tier", "baseline_value", "risk_adjusted_value",
                              "recommended_bid_ceiling", "let_go_threshold",
                              "elite_anchor_weight", "positional_scarcity_modifier",
                              "value_gap", "value_gap_signal", "data_confidence"])
    player.position     = position
    player.market_value = Decimal(str(market_value)) if market_value is not None else None

    profile = MagicMock()
    profile.clean_season_baseline = {"ppr_points": ppr_points}
    player.profile = profile

    if risk_modifier is not None:
        inj = MagicMock()
        inj.risk_adjusted_value_modifier = Decimal(str(risk_modifier))
        player.injury_profile = inj
    else:
        player.injury_profile = None

    return player


async def test_all_top_200_players_have_bid_ceiling():
    """
    When 200 players with valid profiles are processed, every one should
    have recommended_bid_ceiling set (not None, not skipped).
    """
    # Build 200 mock WRs with descending PPR points
    mock_players = [
        _make_player("WR", ppr_points=300 - i * 0.5) for i in range(200)
    ]

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__  = AsyncMock(return_value=False)
    session.execute    = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add    = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    assert result["updated"] == 200
    assert result["skipped"] == 0
    # Verify each player had recommended_bid_ceiling set (attribute was assigned)
    for p in mock_players:
        assert hasattr(p, "recommended_bid_ceiling")


async def test_run_valuation_pass_skips_players_without_ppr():
    """Players with no profile or zero ppr_points are skipped (not written)."""
    wr_with_profile = _make_player("WR", ppr_points=200)
    wr_no_profile   = _make_player("WR", ppr_points=0)
    wr_no_profile.profile = None

    mock_players = [wr_with_profile, wr_no_profile]

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__  = AsyncMock(return_value=False)
    session.execute    = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add    = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    assert result["updated"] == 1
    assert result["skipped"] == 1


async def test_run_valuation_pass_non_skill_positions_ignored():
    """K and DEF players are not in DRAFTABLE_POSITIONS — not processed."""
    k   = _make_player("K",   ppr_points=50)
    def_ = _make_player("DEF", ppr_points=120)
    wr  = _make_player("WR",  ppr_points=200)
    mock_players = [k, def_, wr]

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__  = AsyncMock(return_value=False)
    session.execute    = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add    = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    assert result["updated"] == 1


async def test_run_valuation_pass_returns_analysis_year():
    """Return dict must include analysis_year from get_analysis_year()."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__  = AsyncMock(return_value=False)
    session.execute    = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
    )
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    assert "analysis_year" in result
    assert isinstance(result["analysis_year"], int)
    assert result["analysis_year"] > 2020


# ===========================================================================
# Hardcoded year check
# ===========================================================================

def test_analysis_year_dynamic_not_hardcoded():
    """No hardcoded year literals (2023, 2024, 2025, 2026...) in valuation.py."""
    valuation_path = (
        Path(__file__).parent.parent.parent.parent
        / "backend" / "engines" / "valuation.py"
    )
    source = valuation_path.read_text()
    # Pattern: 4-digit year from 2020-2030 as a standalone number
    matches = re.findall(r"\b20(2[0-9]|30)\b", source)
    assert not matches, (
        f"Hardcoded year found in valuation.py: {matches}. "
        "Use get_analysis_year() from backend.utils.seasons instead."
    )
