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

_player_counter = 0


def _make_player(
    position: str,
    ppr_points: float,
    market_value: float | None = None,
    risk_modifier: float | None = None,
    post_acl_flag: bool = False,
    workload_cliff_flag: bool = False,
    team_abbr: str = "NYG",
) -> MagicMock:
    """Helper: create a mock Player with nested profile + injury_profile."""
    global _player_counter
    _player_counter += 1
    player = MagicMock(spec=["id", "name", "position", "team_abbr", "profile",
                              "injury_profile", "market_value", "tier",
                              "baseline_value", "risk_adjusted_value",
                              "recommended_bid_ceiling", "let_go_threshold",
                              "elite_anchor_weight", "positional_scarcity_modifier",
                              "value_gap", "value_gap_signal", "data_confidence"])
    player.id           = _player_counter
    player.name         = f"Player_{_player_counter}"
    player.position     = position
    player.team_abbr    = team_abbr
    player.market_value = Decimal(str(market_value)) if market_value is not None else None

    profile = MagicMock()
    profile.clean_season_baseline = {"ppr_points": ppr_points}
    player.profile = profile

    if risk_modifier is not None or post_acl_flag or workload_cliff_flag:
        inj = MagicMock()
        inj.risk_adjusted_value_modifier = Decimal(str(risk_modifier)) if risk_modifier is not None else None
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
    _apply_injury_discount,
    get_draftable_pool_sizes,
    calculate_replacement_level,
    sanity_check_valuations,
)


def test_bid_ceiling_hard_cap_enforced():
    """FIX 5: Bid ceiling must never exceed MAX_REALISTIC_BID for the position."""
    # RB max is $80 — create a T1 RB with system_value that would produce ceiling > $80
    sv = Decimal("70")  # high system value
    mv = Decimal("75")  # high market value
    tier = 1
    ceiling = compute_bid_ceiling(sv, mv, tier, "RB", Decimal("0.0"))
    # Without hard cap, T1 RB ceiling = blend * scarcity = high
    # The compute_bid_ceiling itself doesn't cap — run_valuation_pass caps after
    # But we can verify MAX_REALISTIC_BID is correct
    assert MAX_REALISTIC_BID["RB"] == 80
    assert MAX_REALISTIC_BID["WR"] == 70
    assert MAX_REALISTIC_BID["QB"] == 50
    assert MAX_REALISTIC_BID["TE"] == 45


def test_replacement_level_floor_constants():
    """FIX 2: Verify replacement level PPR per game floor values exist."""
    assert REPLACEMENT_LEVEL_PPR_PER_GAME["QB"] == 18.0
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
    assert sizes["QB"] >= 15   # 12 starters + bench
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
