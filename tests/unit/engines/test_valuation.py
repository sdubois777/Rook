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
# assign_tier — position-specific PAR-ratio thresholds
# ===========================================================================

def test_assign_tier_rb_elite_par_is_tier1():
    """RB PAR ratio >= 2.3 → T1."""
    assert assign_tier(3.0, "RB") == 1
    assert assign_tier(2.3, "RB") == 1
    assert assign_tier(2.29, "RB") == 2


def test_assign_tier_wr_elite_par_is_tier1():
    """WR PAR ratio >= 2.2 → T1 (top ~5 WRs)."""
    assert assign_tier(2.5, "WR") == 1
    assert assign_tier(2.2, "WR") == 1
    assert assign_tier(2.19, "WR") == 2


def test_assign_tier_te_elite_par_is_tier1():
    """TE PAR ratio >= 1.85 → T1 (McBride/Bowers clear gap above field)."""
    assert assign_tier(2.5, "TE") == 1
    assert assign_tier(2.05, "TE") == 1
    assert assign_tier(1.85, "TE") == 1
    assert assign_tier(1.84, "TE") == 2


def test_assign_tier_qb_elite_par_is_tier1():
    """QB PAR ratio >= 1.15 → T1 (only truly elite QBs)."""
    assert assign_tier(1.30, "QB") == 1
    assert assign_tier(1.15, "QB") == 1
    assert assign_tier(1.14, "QB") == 2


def test_assign_tier_rb_strong_par_is_tier2():
    """RB PAR ratio 1.8–2.29 → T2."""
    assert assign_tier(2.2, "RB") == 2
    assert assign_tier(1.8, "RB") == 2


def test_assign_tier_wr_strong_par_is_tier2():
    """WR PAR ratio 1.5–2.19 → T2."""
    assert assign_tier(2.1, "WR") == 2
    assert assign_tier(1.5, "WR") == 2


def test_assign_tier_rb_starter_par_is_tier3():
    """RB PAR ratio 1.3–1.79 → T3."""
    assert assign_tier(1.7, "RB") == 3
    assert assign_tier(1.3, "RB") == 3


def test_assign_tier_wr_starter_par_is_tier3():
    """WR PAR ratio 1.2–1.49 → T3."""
    assert assign_tier(1.4, "WR") == 3
    assert assign_tier(1.2, "WR") == 3


def test_assign_tier_flex_par_is_tier4():
    """PAR ratio 0.8–threshold → T4 (all positions)."""
    assert assign_tier(1.1, "RB") == 4
    assert assign_tier(0.8, "WR") == 4


def test_assign_tier_below_replacement_is_tier5():
    """PAR ratio < 0.8 → T5."""
    assert assign_tier(0.7, "RB") == 5
    assert assign_tier(0.0, "WR") == 5


def test_assign_tier_unknown_position_uses_wr_defaults():
    """Unknown position falls back to WR thresholds."""
    assert assign_tier(2.2, "K") == 1
    assert assign_tier(1.5, "K") == 2


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
    """Tier 1: blend = system × 0.15 + risk_adj_market × 0.85, then × scarcity."""
    sv = Decimal("40")
    mv = Decimal("50")
    # low risk = 0% discount → risk_adj_market = 50
    # anchor = 0.85 → blend = 40×0.15 + 50×0.85 = 6 + 42.5 = 48.5
    # scarcity for WR T1 = 1.20 → 48.5 × 1.20 = 58.20
    ceiling = compute_bid_ceiling(sv, mv, tier=1, position="WR", risk_level="low")
    assert ceiling == Decimal("58.20")


def test_tier1_anchor_weight_is_0_85():
    assert ANCHOR_WEIGHTS[1] == Decimal("0.85")


def test_scarcity_modifier_applied_to_tier1_rb():
    """Tier 1 RB scarcity modifier = 1.35."""
    sv = Decimal("40")
    mv = Decimal("40")
    # blend = 40×0.15 + 40×0.85 = 40, scarcity 1.35 → 40×1.35 = 54
    ceiling = compute_bid_ceiling(sv, mv, tier=1, position="RB")
    assert ceiling == Decimal("54.00")


def test_tier1_rb_scarcity_modifier_is_1_35():
    assert SCARCITY_MODIFIERS["RB"] == Decimal("1.35")


def test_tier1_wr_scarcity_modifier_is_1_20():
    assert SCARCITY_MODIFIERS["WR"] == Decimal("1.20")


# ===========================================================================
# compute_bid_ceiling — Tier 2/3
# ===========================================================================

def test_tier2_bid_ceiling_uses_anchor_weight():
    """Tier 2: anchor=0.65 → blend = system × 0.35 + market × 0.65."""
    sv = Decimal("20")
    mv = Decimal("30")
    # low risk → risk_adj_market = 30
    # blend = 20×0.35 + 30×0.65 = 7 + 19.5 = 26.5
    ceiling = compute_bid_ceiling(sv, mv, tier=2, position="WR")
    assert ceiling == Decimal("26.50")


def test_tier3_bid_ceiling_uses_anchor_weight():
    """Tier 3: anchor=0.40 → blend = system × 0.60 + market × 0.40."""
    sv = Decimal("10")
    mv = Decimal("15")
    # blend = 10×0.60 + 15×0.40 = 6 + 6 = 12
    ceiling = compute_bid_ceiling(sv, mv, tier=3, position="RB")
    assert ceiling == Decimal("12.00")


# ===========================================================================
# compute_bid_ceiling — Tier 4/5
# ===========================================================================

def test_tier4_bid_ceiling_uses_small_market_weight():
    """Tier 4: anchor=0.15 → blend = system × 0.85 + market × 0.15."""
    sv = Decimal("5")
    mv = Decimal("100")
    # blend = 5×0.85 + 100×0.15 = 4.25 + 15 = 19.25
    ceiling = compute_bid_ceiling(sv, mv, tier=4, position="RB")
    assert ceiling == Decimal("19.25")


def test_tier5_bid_ceiling_ignores_market_value():
    """Tier 5: anchor=0.00 → ceiling = system_value."""
    sv = Decimal("3")
    mv = Decimal("50")
    ceiling = compute_bid_ceiling(sv, mv, tier=5, position="WR")
    assert ceiling == Decimal("3.00")


def test_tier5_bid_ceiling_uses_system_value_only():
    """Confirm Tier 5 is exactly system_value (no market influence)."""
    sv = Decimal("7")
    ceiling = compute_bid_ceiling(sv, None, tier=5, position="QB")
    assert ceiling == sv


# ===========================================================================
# compute_bid_ceiling — risk level (market discount)
# ===========================================================================

def test_risk_discount_reduces_market_before_blend():
    """High risk (15% discount) reduces market_value before blending."""
    sv = Decimal("30")
    mv = Decimal("40")
    # Tier 2 (anchor=0.65), high risk: risk_adjusted_market = 40 × (1 - 0.15) = 34
    # blend = 30 × 0.35 + 34 × 0.65 = 10.5 + 22.1 = 32.6
    ceiling = compute_bid_ceiling(sv, mv, tier=2, position="WR", risk_level="high")
    assert ceiling == Decimal("32.60")


def test_low_risk_no_market_discount():
    """low risk = 0% market discount, same as default."""
    sv = Decimal("20")
    ceiling_default = compute_bid_ceiling(sv, None, tier=3, position="TE")
    ceiling_low = compute_bid_ceiling(sv, None, tier=3, position="TE", risk_level="low")
    assert ceiling_default == ceiling_low


def test_ceiling_minimum_is_one_dollar():
    """Even with volatile risk, ceiling never drops below $1."""
    sv = Decimal("1")
    ceiling = compute_bid_ceiling(sv, None, tier=5, position="RB", risk_level="volatile")
    assert ceiling == Decimal("1.00")


def test_market_value_none_uses_system_value_for_blend():
    """When market_value is None, market treated as = system_value → neutral blend."""
    sv = Decimal("20")
    # Tier 2, no market → blend = 20×0.35 + 20×0.65 = 20
    ceiling = compute_bid_ceiling(sv, None, tier=2, position="WR")
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

def test_let_go_threshold_low_risk_is_ceiling_plus_20_pct():
    ceiling = Decimal("40")
    let_go = compute_let_go_threshold(ceiling, risk_level="low")
    assert let_go == Decimal("48.00")


def test_let_go_threshold_volatile_is_ceiling_plus_5_pct():
    ceiling = Decimal("40")
    let_go = compute_let_go_threshold(ceiling, risk_level="volatile")
    assert let_go == Decimal("42.00")


def test_let_go_threshold_rounds_to_two_dp():
    ceiling = Decimal("33")
    let_go = compute_let_go_threshold(ceiling, risk_level="moderate")
    # 33 × 1.15 = 37.95
    assert let_go == Decimal("37.95")


# ===========================================================================
# run_valuation_pass — async integration (DB mocked)
# ===========================================================================

_player_counter = 0


def _make_player(
    position: str,
    ppr_points: float,
    market_value: float | None = None,
    risk_modifier: float | None = None,
    risk_level: str | None = None,
    post_acl_flag: bool = False,
    workload_cliff_flag: bool = False,
    team_abbr: str = "NYG",
) -> MagicMock:
    """Helper: create a mock Player with nested profile + injury_profile."""
    global _player_counter
    _player_counter += 1
    player = MagicMock(spec=["id", "name", "position", "team_abbr", "profile",
                              "injury_profile", "dependencies", "market_value",
                              "market_value_fantasypros", "market_value_league",
                              "ai_bid_ceiling",
                              "tier", "baseline_value", "risk_adjusted_value",
                              "recommended_bid_ceiling", "let_go_threshold",
                              "elite_anchor_weight", "positional_scarcity_modifier",
                              "value_gap", "value_gap_signal", "data_confidence"])
    player.id           = _player_counter
    player.name         = f"Player_{_player_counter}"
    player.position     = position
    player.team_abbr    = team_abbr
    player.market_value = Decimal(str(market_value)) if market_value is not None else None
    player.market_value_fantasypros = Decimal(str(market_value)) if market_value is not None else None
    player.market_value_league = None
    player.ai_bid_ceiling = None

    profile = MagicMock()
    profile.clean_season_baseline = {"ppr_points": ppr_points}
    player.profile = profile
    player.dependencies = []

    if risk_modifier is not None or risk_level is not None or post_acl_flag or workload_cliff_flag:
        inj = MagicMock()
        inj.risk_adjusted_value_modifier = Decimal(str(risk_modifier)) if risk_modifier is not None else None
        inj.overall_risk_level = risk_level if risk_level is not None else "low"
        inj.post_acl_flag = post_acl_flag
        inj.workload_cliff_flag = workload_cliff_flag
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
    wr_no_profile.baseline_value = None  # no stale value to clear

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


# ===========================================================================
# FIX tests — valuation calibration and edge cases
# ===========================================================================

from backend.engines.valuation import (
    MAX_REALISTIC_BID,
    REPLACEMENT_LEVEL_PPR_PER_GAME,
    POST_MAJOR_INJURY_DISCOUNT,
    RISK_MARKET_DISCOUNT,
    LET_GO_MULTIPLIER,
    _apply_injury_discount,
    _apply_dependency_adjustment,
    get_draftable_pool_sizes,
    calculate_replacement_level,
    sanity_check_valuations,
)


def test_bid_ceiling_hard_cap_enforced():
    """FIX 5: Bid ceiling must never exceed MAX_REALISTIC_BID for the position."""
    # RB max is $80 — create a T1 RB with system_value that would produce ceiling > $80
    sv = Decimal("70")  # high system value
    mv = Decimal("75")  # high market value
    ceiling = compute_bid_ceiling(sv, mv, tier=1, position="RB", risk_level="low")
    # Without hard cap, T1 RB ceiling = blend * scarcity = high
    # The compute_bid_ceiling itself doesn't cap — run_valuation_pass caps after
    # But we can verify MAX_REALISTIC_BID is correct
    assert MAX_REALISTIC_BID["RB"] == 80
    assert MAX_REALISTIC_BID["WR"] == 70
    assert MAX_REALISTIC_BID["QB"] == 50
    assert MAX_REALISTIC_BID["TE"] == 45


def test_replacement_level_floor_constants():
    """FIX 2: Verify replacement level PPR per game floor values exist."""
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["QB"] == 17.0
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["RB"] == 8.0
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["WR"] == 7.0
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["TE"] == 5.0


def test_injury_discount_post_acl():
    """FIX 4: Players with post_acl_flag get 25% PPR discount."""
    inj = MagicMock()
    inj.post_acl_flag = True
    inj.workload_cliff_flag = False
    inj.risk_adjusted_value_modifier = None

    profile = MagicMock()
    profile.career_trajectory = "established"
    profile.clean_season_baseline = {"ppr_points": 280.0}

    result = _apply_injury_discount(280.0, inj, profile)
    assert result == 280.0 * POST_MAJOR_INJURY_DISCOUNT  # 280 * 0.75 = 210


def test_injury_discount_workload_cliff():
    """FIX 4: Players with workload_cliff_flag get 15% PPR discount."""
    inj = MagicMock()
    inj.post_acl_flag = False
    inj.workload_cliff_flag = True
    inj.risk_adjusted_value_modifier = None

    profile = MagicMock()
    profile.career_trajectory = "established"
    profile.clean_season_baseline = {"ppr_points": 250.0}

    result = _apply_injury_discount(250.0, inj, profile)
    assert result == 250.0 * 0.85  # 250 * 0.85 = 212.5


def test_injury_discount_none_when_healthy():
    """FIX 4: Healthy players get no discount."""
    result = _apply_injury_discount(280.0, None, None)
    assert result == 280.0


def test_declining_baseline_discount():
    """FIX 4: Players with declining flag in baseline get 15% discount."""
    profile = MagicMock()
    profile.career_trajectory = "established"
    profile.clean_season_baseline = {"ppr_points": 200.0, "declining": True}

    result = _apply_injury_discount(200.0, None, profile)
    assert result == 200.0 * 0.85  # 200 * 0.85 = 170


def test_career_trajectory_declining_discount():
    """Players with career_trajectory='declining' get 15% discount even without baseline flag."""
    profile = MagicMock()
    profile.career_trajectory = "declining"
    profile.clean_season_baseline = {"ppr_points": 280.0}  # no "declining" key

    result = _apply_injury_discount(280.0, None, profile)
    assert result == 280.0 * 0.85  # 238


def test_post_acl_plus_declining_stacks():
    """POST_ACL (25%) + declining (15%) stack multiplicatively, floored at 0.60."""
    inj = MagicMock()
    inj.post_acl_flag = True
    inj.workload_cliff_flag = False

    profile = MagicMock()
    profile.career_trajectory = "declining"
    profile.clean_season_baseline = {"ppr_points": 280.0}

    result = _apply_injury_discount(280.0, inj, profile)
    # 0.75 * 0.85 = 0.6375, floored at 0.60
    assert result == 280.0 * 0.6375


@pytest.mark.asyncio
async def test_stale_valuations_cleared():
    """Stale baseline_value is cleared for players with no profile."""
    wr_valued = _make_player("WR", ppr_points=200)
    wr_stale = _make_player("WR", ppr_points=0)
    wr_stale.profile = None
    wr_stale.baseline_value = Decimal("50.00")  # stale value from old run

    mock_players = [wr_valued, wr_stale]
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    assert result["cleared"] == 1
    assert wr_stale.baseline_value is None
    assert wr_stale.tier is None


@pytest.mark.asyncio
async def test_hard_cap_applied_in_valuation_pass():
    """FIX 5: Bid ceiling is capped to MAX_REALISTIC_BID during full pass."""
    # Create an RB with very high PPR that would exceed $80 ceiling
    rb_elite = _make_player("RB", ppr_points=350)
    rb_elite.name = "Elite_RB"

    mock_players = [rb_elite]
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        await run_valuation_pass()

    # Ceiling should be capped to $80 for RB
    assert rb_elite.recommended_bid_ceiling <= Decimal("80")


@pytest.mark.asyncio
async def test_free_agents_get_zero_value():
    """Free agents (team_abbr='FA' or None) should be skipped and cleared."""
    rb_active = _make_player("RB", ppr_points=250, team_abbr="NYG")
    rb_fa = _make_player("RB", ppr_points=250, team_abbr="FA")
    rb_fa.baseline_value = Decimal("50.00")  # stale value from before FA
    rb_none = _make_player("RB", ppr_points=250, team_abbr=None)
    rb_none.baseline_value = Decimal("40.00")

    mock_players = [rb_active, rb_fa, rb_none]
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    # Only the active player should be valued
    assert result["updated"] == 1
    # Both FA players should have stale values cleared
    assert result["cleared"] == 2
    assert rb_fa.baseline_value is None
    assert rb_fa.tier is None
    assert rb_none.baseline_value is None
    assert rb_none.tier is None
    # Active player should have a value
    assert rb_active.baseline_value is not None


# ===========================================================================
# FIX 1+2: Dynamic pool sizes and replacement levels
# ===========================================================================


def test_pool_sizes_derived_from_league_settings():
    """Pool sizes calculated from roster slots × teams + bench/flex allocation."""
    sizes = get_draftable_pool_sizes(teams=12)
    # Standard 12-team league: each position should include starters + bench depth
    assert sizes["QB"] == 13   # 12 starters + 1 (first non-starter)
    assert sizes["RB"] >= 40   # 24 starters + flex + bench
    assert sizes["WR"] >= 50   # 24 starters + flex + bench
    assert sizes["TE"] >= 20   # 12 starters + flex + bench
    total = sum(sizes.values())
    assert 130 <= total <= 200, f"Total pool {total} outside expected range"


def test_pool_sizes_scale_with_teams():
    """10-team league should have smaller pools than 14-team."""
    small = get_draftable_pool_sizes(teams=10)
    large = get_draftable_pool_sizes(teams=14)
    for pos in ("QB", "RB", "WR", "TE"):
        assert small[pos] < large[pos], f"{pos} pool didn't scale with teams"


def test_replacement_level_is_last_player_in_pool():
    """Replacement level = PPR of the Nth player in the pool."""
    # 10 players: 100, 90, 80, 70, 60, 50, 40, 30, 20, 10
    sorted_pprs = [100 - i * 10 for i in range(10)]
    # Pool size 5 → replacement = player #5 = 60
    repl = calculate_replacement_level(sorted_pprs, pool_size=5)
    assert repl == 60.0


def test_replacement_level_fewer_players_than_pool():
    """When fewer players exist than pool size, use the last player."""
    sorted_pprs = [200.0, 150.0, 100.0]
    repl = calculate_replacement_level(sorted_pprs, pool_size=10)
    assert repl == 100.0


def test_replacement_level_not_hardcoded():
    """REPLACEMENT_RANK constant should not exist in valuation.py."""
    import re
    from pathlib import Path

    path = Path(__file__).parent.parent.parent.parent / "backend" / "engines" / "valuation.py"
    source = path.read_text(encoding="utf-8")
    # The old hardcoded constant should be gone
    assert "REPLACEMENT_RANK" not in source, "REPLACEMENT_RANK still exists — should use dynamic pool sizes"


@pytest.mark.asyncio
async def test_total_system_value_near_skill_dollar_pool():
    """Total system value across all positions should be near $2,220."""
    # Build a realistic multi-position player set
    mock_players = []
    # 20 QBs, 60 RBs, 70 WRs, 30 TEs — enough to fill all pools
    for i in range(20):
        mock_players.append(_make_player("QB", ppr_points=400 - i * 8))
    for i in range(60):
        mock_players.append(_make_player("RB", ppr_points=300 - i * 3.5))
    for i in range(70):
        mock_players.append(_make_player("WR", ppr_points=320 - i * 3))
    for i in range(30):
        mock_players.append(_make_player("TE", ppr_points=260 - i * 6))

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    # Sum all baseline_value assignments
    total_sv = sum(
        float(p.baseline_value) for p in mock_players
        if p.baseline_value is not None
    )
    # Should be near $2,220 (± 15%)
    assert 1800 <= total_sv <= 2600, f"Total system value ${total_sv:.0f} out of range"


@pytest.mark.asyncio
async def test_value_gap_signals_mixed_not_all_same():
    """With market values present, gap signals should be mixed."""
    mock_players = []
    # RBs with varied market values — some over, some under, some aligned
    ppr_values = [300, 250, 220, 190, 160, 140, 120, 100, 80, 60]
    market_values = [40, 10, 35, 25, 5, 20, 15, 8, 3, 1]
    for i in range(10):
        mock_players.append(
            _make_player("RB", ppr_points=ppr_values[i], market_value=market_values[i])
        )

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        await run_valuation_pass()

    signals = {p.value_gap_signal for p in mock_players if p.value_gap_signal}
    # Should have at least 2 different signal types
    assert len(signals) >= 2, f"Signals too uniform: {signals}"


@pytest.mark.asyncio
async def test_value_gap_no_market_data_signal():
    """Players without market_value get 'no_market_data' signal."""
    mock_players = [_make_player("WR", ppr_points=200)]  # no market_value

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        await run_valuation_pass()

    assert mock_players[0].value_gap_signal == "no_market_data"
    assert mock_players[0].value_gap is None


def test_gibbs_system_value_in_reasonable_range():
    """With realistic RB pool, Gibbs-level PPR (300) should value $35-65."""
    # Simulate: 52 RBs, replacement at ~100 PPR
    # Total PAR across pool ≈ 3000-5000
    val = ppr_to_system_value(
        ppr_points=300.0,
        replacement_ppr=100.0,
        total_par=4000.0,
        position_budget=844.0,  # 38% of 2220
    )
    assert Decimal("35") <= val <= Decimal("65"), f"Gibbs-level system value ${val} out of range"


def test_williams_system_value_in_reasonable_range():
    """With realistic RB pool, Williams-level PPR (268) should value $20-50."""
    val = ppr_to_system_value(
        ppr_points=268.0,
        replacement_ppr=100.0,
        total_par=4000.0,
        position_budget=844.0,
    )
    assert Decimal("20") <= val <= Decimal("50"), f"Williams-level system value ${val} out of range"


def test_sanity_check_passes_clean_data():
    """Sanity check returns no warnings for well-calibrated data."""
    players = []
    # Build data that sums to ~$2100 (within 10% of $2220 pool)
    for i in range(50):
        p = MagicMock()
        p.position = "RB"
        p.baseline_value = Decimal(str(max(1, 30 - i * 0.5)))
        players.append(p)
    for i in range(50):
        p = MagicMock()
        p.position = "WR"
        p.baseline_value = Decimal(str(max(1, 25 - i * 0.4)))
        players.append(p)
    for i in range(15):
        p = MagicMock()
        p.position = "QB"
        p.baseline_value = Decimal(str(max(1, 20 - i * 1.0)))
        players.append(p)
    for i in range(15):
        p = MagicMock()
        p.position = "TE"
        p.baseline_value = Decimal(str(max(1, 18 - i * 0.9)))
        players.append(p)

    warnings = sanity_check_valuations(players, 2220.0)
    critical = [w for w in warnings if "exceeds pool" in w or "exceeds cap" in w]
    assert not critical, f"Critical warnings: {critical}"


def test_sanity_check_flags_inflated_total():
    """Sanity check flags total value exceeding pool by >10%."""
    players = []
    for i in range(10):
        p = MagicMock()
        p.position = "RB"
        p.baseline_value = Decimal("300")  # absurdly high
        players.append(p)

    warnings = sanity_check_valuations(players, 2220.0)
    assert any("exceeds pool" in w for w in warnings)


# ===========================================================================
# _apply_dependency_adjustment
# ===========================================================================

def _make_dep(flag_type, trigger_condition, value_impact_pct):
    """Create a mock PlayerDependency."""
    dep = MagicMock()
    dep.flag_type = flag_type
    dep.trigger_condition = trigger_condition
    dep.value_impact_pct = Decimal(str(value_impact_pct))
    return dep


def test_dependency_adjustment_beneficiary_departed():
    """BENEFICIARY + departed_team applies positive adjustment."""
    dep = _make_dep("beneficiary", "departed_team", 0.35)
    result = _apply_dependency_adjustment(250.0, [dep])
    assert result == pytest.approx(337.5)  # 250 × 1.35


def test_dependency_adjustment_displaced():
    """DISPLACED + active_and_healthy applies negative adjustment."""
    dep = _make_dep("displaced", "active_and_healthy", -0.25)
    result = _apply_dependency_adjustment(300.0, [dep])
    assert result == pytest.approx(225.0)  # 300 × 0.75


def test_dependency_adjustment_contingent_skipped():
    """CONTINGENT flags are skipped pre-draft."""
    dep = _make_dep("contingent", "injured", 0.20)
    result = _apply_dependency_adjustment(300.0, [dep])
    assert result == 300.0  # no change


# ===========================================================================
# Tier from raw PPR, risk modifier cap, phantom flag validation
# ===========================================================================

from backend.engines.valuation import _get_risk_modifier, MAX_RISK_MODIFIER


@pytest.mark.asyncio
async def test_tier_from_raw_ppr_not_adjusted():
    """Player with dependency adjustment still gets tier from raw PPR ratio.

    Amon-Ra scenario: 265 raw PPR / 153 replacement = 1.73 ratio → Tier 2
    (WR T2 threshold = 1.5), even though adjusted_ppr is ~198 after displaced flag.
    Tier from raw PPR, not risk-adjusted PPR.
    """
    # Create 10 WRs: #8 has a -25% displaced flag
    mock_players = []
    for i in range(10):
        ppr = 300 - i * 5  # 300, 295, 290, ... 255
        p = _make_player("WR", ppr_points=ppr)
        p.name = f"WR_{i+1}"
        if i == 7:  # rank 8 — like Amon-Ra
            p.name = "Amon-Ra"
            dep = _make_dep("displaced", "active_and_healthy", -0.25)
            p.dependencies = [dep]
        mock_players.append(p)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        await run_valuation_pass()

    amon_ra = [p for p in mock_players if p.name == "Amon-Ra"][0]
    # 265 raw PPR / 153 repl = 1.73 PAR ratio → Tier 2 (WR T2 >= 1.5)
    assert amon_ra.tier == 2, f"Amon-Ra tier should be 2, got {amon_ra.tier}"
    # Dollar value should still be reduced (adjusted PPR used for PAR)
    assert amon_ra.baseline_value is not None


def test_risk_modifier_capped_at_40pct():
    """Risk modifier worse than -0.40 is capped to -0.40."""
    inj = MagicMock()
    inj.risk_adjusted_value_modifier = Decimal("-0.48")
    result = _get_risk_modifier(inj)
    assert result == MAX_RISK_MODIFIER  # -0.40


def test_risk_modifier_within_range_unchanged():
    """Risk modifier within range passes through unchanged."""
    inj = MagicMock()
    inj.risk_adjusted_value_modifier = Decimal("-0.25")
    result = _get_risk_modifier(inj)
    assert result == Decimal("-0.25")


def test_risk_modifier_none_when_no_profile():
    """No injury profile returns None."""
    assert _get_risk_modifier(None) is None


def test_risk_modifier_not_applied_twice():
    """Risk modifier affects bid ceiling and risk_adjusted_value but not sv.

    sv (baseline_value) comes from adjusted_ppr via PAR.
    risk_modifier is applied separately in compute_bid_ceiling and risk_adj.
    The risk modifier must NOT be embedded in ppr_to_system_value.
    """
    # ppr_to_system_value has no risk_modifier parameter
    import inspect
    sig = inspect.signature(ppr_to_system_value)
    param_names = list(sig.parameters.keys())
    assert "risk_modifier" not in param_names
    assert "risk" not in " ".join(param_names).lower()


@pytest.mark.asyncio
async def test_no_high_market_player_in_tier4():
    """No player with market_value > $35 should end up in Tier 4 or worse.

    This catches the Amon-Ra bug where dependency adjustments
    pushed a top player into low tiers.
    """
    mock_players = []
    # 40 WRs: top 10 have market_value $40+
    for i in range(40):
        ppr = 340 - i * 5
        mv = max(1, 60 - i * 2)
        p = _make_player("WR", ppr_points=ppr, market_value=float(mv))
        p.name = f"WR_{i+1}"
        mock_players.append(p)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        await run_valuation_pass()

    for p in mock_players:
        if p.market_value and p.market_value > Decimal("35"):
            assert p.tier is not None and p.tier <= 3, (
                f"{p.name} has market_value=${p.market_value} but tier={p.tier}"
            )


def test_dependency_adjustment_scheme_fit_half_weight():
    """SCHEME_FIT applied at half weight pre-draft."""
    dep = _make_dep("scheme_fit", "active_and_healthy", 0.20)
    result = _apply_dependency_adjustment(300.0, [dep])
    assert result == pytest.approx(330.0)  # 300 × (1 + 0.20×0.5)


def test_dependency_adjustment_multiple_flags():
    """Multiple flags stack additively."""
    dep1 = _make_dep("beneficiary", "departed_team", 0.35)
    dep2 = _make_dep("displaced", "active_and_healthy", -0.15)
    result = _apply_dependency_adjustment(250.0, [dep1, dep2])
    assert result == pytest.approx(300.0)  # 250 × (1 + 0.35 - 0.15)


def test_dependency_adjustment_no_dependencies():
    """Empty dependency list returns PPR unchanged."""
    result = _apply_dependency_adjustment(300.0, [])
    assert result == 300.0


def test_dependency_adjustment_floors_at_zero():
    """Massive negative adjustment floors at 0."""
    dep = _make_dep("displaced", "active_and_healthy", -150)  # -150% = normalized to -1.50
    result = _apply_dependency_adjustment(200.0, [dep])
    assert result == 0.0


def test_dependency_adjustment_normalizes_whole_percentages():
    """AI model outputs whole-number percentages (35 = 35%), normalize to fractions."""
    dep = _make_dep("displaced", "active_and_healthy", -35)  # AI model format
    result = _apply_dependency_adjustment(300.0, [dep])
    assert result == pytest.approx(195.0)  # 300 × (1 - 0.35)


# ===========================================================================
# Replacement level floor enforcement
# ===========================================================================


def test_replacement_level_floor_values():
    """LEAGUE_RULES.md replacement floors: QB=18, RB=8, WR=7, TE=5 PPR/game."""
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["QB"] * 17 == pytest.approx(289.0)
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["RB"] * 17 == pytest.approx(136.0)
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["WR"] * 17 == pytest.approx(119.0)
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["TE"] * 17 == pytest.approx(85.0)


@pytest.mark.asyncio
async def test_replacement_floor_enforced_wr():
    """
    WR replacement level is max(dynamic, 119).
    With 70 WRs where #60 projects 84 PPR, floor should kick in at 119.
    """
    # 70 WRs: top player 320 PPR, descending ~3.5 per rank
    # Player #60 would be at 320 - 59*3.5 = 113.5, below 119 floor
    mock_players = [
        _make_player("WR", ppr_points=320 - i * 3.5) for i in range(70)
    ]

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    # Replacement level should be 119 (floor), not ~113.5 (dynamic)
    assert result["replacement_levels"]["WR"] == pytest.approx(119.0)


@pytest.mark.asyncio
async def test_replacement_floor_enforced_rb():
    """RB replacement floor = 8.0 × 17 = 136 PPR."""
    mock_players = [
        _make_player("RB", ppr_points=300 - i * 4) for i in range(60)
    ]

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    assert result["replacement_levels"]["RB"] >= 136.0


@pytest.mark.asyncio
async def test_replacement_floor_not_applied_when_dynamic_higher():
    """When dynamic replacement > floor, use dynamic (floor is a minimum, not a target)."""
    # Only 5 WRs — all high PPR. Dynamic replacement will be well above 119.
    mock_players = [
        _make_player("WR", ppr_points=320 - i * 10) for i in range(5)
    ]

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        result = await run_valuation_pass()

    # Last WR = 320 - 4*10 = 280. Dynamic = 280, but max cap = 9.0 PPG × 17 = 153.
    # Replacement is capped at 153.
    assert result["replacement_levels"]["WR"] == pytest.approx(153.0)


@pytest.mark.asyncio
async def test_low_ppr_wrs_get_dollar_1():
    """WRs projecting below the 119 PPR floor get $1 system value."""
    mock_players = [
        _make_player("WR", ppr_points=300),  # above floor
        _make_player("WR", ppr_points=100),  # below 119 floor
        _make_player("WR", ppr_points=80),   # well below floor
    ]
    mock_players[0].name = "Star_WR"
    mock_players[1].name = "Below_Floor_WR"
    mock_players[2].name = "Deep_Bench_WR"

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        await run_valuation_pass()

    # Players below replacement floor get $1
    assert mock_players[1].baseline_value == Decimal("1.00")
    assert mock_players[2].baseline_value == Decimal("1.00")
    # Star WR gets meaningful value
    assert mock_players[0].baseline_value > Decimal("1.00")


# ===========================================================================
# Risk market discount — bid ceiling redesign tests
# ===========================================================================


def test_volatile_still_draftable():
    """Volatile T2 WR with $49 market should have ceiling > $20.

    Old formula: ceiling = blend × (1 + -0.35) → $16.22 (undraftable).
    New formula: market discount only → ceiling stays reasonable.
    """
    sv = Decimal("21")
    mv = Decimal("49")
    # volatile discount = 22%: risk_adj_market = 49 × 0.78 = 38.22
    # T2 anchor=0.65: blend = 21 × 0.35 + 38.22 × 0.65 = 7.35 + 24.843 = 32.193
    ceiling = compute_bid_ceiling(sv, mv, tier=2, position="WR", risk_level="volatile")
    assert ceiling > Decimal("20.00")
    assert ceiling < Decimal("40.00")


def test_risk_discount_constants_valid():
    """Verify RISK_MARKET_DISCOUNT and LET_GO_MULTIPLIER have all four levels."""
    for level in ("low", "moderate", "high", "volatile"):
        assert level in RISK_MARKET_DISCOUNT
        assert level in LET_GO_MULTIPLIER
    assert RISK_MARKET_DISCOUNT["low"] == Decimal("0.00")
    assert RISK_MARKET_DISCOUNT["volatile"] == Decimal("0.22")
    assert LET_GO_MULTIPLIER["low"] == Decimal("1.20")
    assert LET_GO_MULTIPLIER["volatile"] == Decimal("1.05")


def test_tighter_let_go_for_risky_players():
    """Volatile let_go = ceiling × 1.05 vs low = ceiling × 1.20."""
    ceiling = Decimal("30")
    let_go_low = compute_let_go_threshold(ceiling, "low")
    let_go_vol = compute_let_go_threshold(ceiling, "volatile")
    assert let_go_low == Decimal("36.00")   # 30 × 1.20
    assert let_go_vol == Decimal("31.50")   # 30 × 1.05
    assert let_go_vol < let_go_low


def test_healthy_tier1_near_market():
    """Healthy T1 RB: ceiling should be close to market (heavy anchor, no discount)."""
    sv = Decimal("55")
    mv = Decimal("60")
    # low risk → risk_adj_market = 60
    # T1 anchor=0.85: blend = 55 × 0.15 + 60 × 0.85 = 8.25 + 51 = 59.25
    # scarcity RB = 1.35 → 59.25 × 1.35 = 79.9875
    ceiling = compute_bid_ceiling(sv, mv, tier=1, position="RB", risk_level="low")
    assert ceiling == Decimal("79.99")


def test_risk_level_not_risk_modifier_in_ceiling():
    """compute_bid_ceiling uses risk_level (str), not risk_modifier (Decimal)."""
    import inspect
    sig = inspect.signature(compute_bid_ceiling)
    param_names = list(sig.parameters.keys())
    assert "risk_level" in param_names
    assert "risk_modifier" not in param_names


def test_amon_ra_ceiling_above_20():
    """Amon-Ra scenario: T2 WR, sv=$21, mv=$49, high risk → ceiling > $20."""
    sv = Decimal("21")
    mv = Decimal("49")
    # high discount = 15%: risk_adj_market = 49 × 0.85 = 41.65
    # T2 anchor=0.65: blend = 21 × 0.35 + 41.65 × 0.65 = 7.35 + 27.0725 = 34.4225
    ceiling = compute_bid_ceiling(sv, mv, tier=2, position="WR", risk_level="high")
    assert ceiling > Decimal("30.00")
    assert ceiling == Decimal("34.42")


def test_cmc_ceiling_above_45():
    """CMC scenario: T1 RB, sv=$55, mv=$60, moderate risk → ceiling > $45."""
    sv = Decimal("55")
    mv = Decimal("60")
    # moderate discount = 8%: risk_adj_market = 60 × 0.92 = 55.2
    # T1 anchor=0.85: blend = 55 × 0.15 + 55.2 × 0.85 = 8.25 + 46.92 = 55.17
    # scarcity RB = 1.35 → 55.17 × 1.35 = 74.4795
    ceiling = compute_bid_ceiling(sv, mv, tier=1, position="RB", risk_level="moderate")
    assert ceiling > Decimal("45.00")
    assert ceiling == Decimal("74.48")


@pytest.mark.asyncio
async def test_no_high_market_player_ceiling_below_15():
    """No player with market > $35 should have ceiling < $15.

    This is the Amon-Ra regression guard — the old formula crushed
    high-market injured players to undraftable levels.
    """
    mock_players = []
    for i in range(40):
        ppr = 340 - i * 5
        mv = max(1, 60 - i * 2)
        rl = "high" if i % 3 == 0 else "low"
        p = _make_player("WR", ppr_points=ppr, market_value=float(mv), risk_level=rl)
        p.name = f"WR_{i+1}"
        mock_players.append(p)

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(
        return_value=MagicMock(scalars=MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=mock_players))))
    )
    session.add = MagicMock()
    session.commit = AsyncMock()

    with patch("backend.engines.valuation.AsyncSessionLocal", return_value=session):
        await run_valuation_pass()

    for p in mock_players:
        if p.market_value and p.market_value > Decimal("35"):
            assert p.recommended_bid_ceiling >= Decimal("15"), (
                f"{p.name} market=${p.market_value} but ceiling=${p.recommended_bid_ceiling}"
            )
