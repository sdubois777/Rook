"""Distribution-relative tiering (z-score) — adaptivity, format-awareness, K/DEF isolation."""
from decimal import Decimal

import pytest

from backend.engines import valuation as V
from backend.engines.valuation import (
    compute_pool_ztiers, z_to_tier, assign_tier, _Z_TIER_CUTS, _Z_MIN_POOL,
)


def _t1_count(points, pool_size, position="WR"):
    tiers, _, _ = compute_pool_ztiers(sorted(points, reverse=True), pool_size, position)
    assert tiers is not None
    return sum(1 for t in tiers if t == 1)


def test_z_cuts_are_the_documented_values():
    # The one judgment call — locked to the historically-validated +1.25σ T1 cut.
    assert _Z_TIER_CUTS[1] == 1.25


def test_cliff_distribution_produces_small_t1():
    # One dominant player far above a large field (2025 TE shape: McBride alone). The huge
    # gap inflates sigma, so only the outlier clears +1.25σ.
    cliff = [340] + [160 - i for i in range(19)]  # 1 elite, 19-deep field
    assert _t1_count(cliff, pool_size=20, position="TE") == 1


def test_bunched_distribution_produces_larger_t1():
    # Several genuinely-separate players above a large low field (bunched top).
    bunched = [280, 275, 270, 265] + [150 - i for i in range(16)]
    assert _t1_count(bunched, pool_size=20, position="TE") >= 3


def test_cliff_has_fewer_t1_than_bunched():
    # The whole point: SAME cuts, adaptive membership by distribution shape.
    cliff = [340] + [160 - i for i in range(19)]
    bunched = [280, 275, 270, 265] + [150 - i for i in range(16)]
    assert _t1_count(cliff, 20, "TE") < _t1_count(bunched, 20, "TE")


def test_format_awareness_same_cut_different_membership():
    # Reception-stripping (standard) changes distribution SHAPE, so the same z-cut yields
    # format-appropriate tiers with NO per-format constant. Neither format is forced to 0.
    ppr = [300, 280, 270, 250, 240, 235, 230, 225, 220, 215, 210, 205, 200, 195]
    standard = [230, 190, 180, 172, 168, 165, 163, 160, 158, 156, 154, 152, 150, 148]
    assert _t1_count(ppr, 14, "WR") >= 1 and _t1_count(standard, 14, "WR") >= 1


def test_standard_pool_is_not_forced_to_zero_t1():
    # The live bug: absolute thresholds gave Standard WR/TE T1 = 0. z-score never does when
    # a real separator exists.
    standard_wr = [235, 190, 178, 168, 162, 158, 155, 152, 150, 148, 146, 144, 142, 140]
    assert _t1_count(standard_wr, 14, "WR") >= 1


def test_small_pool_falls_back_to_none():
    # Below _Z_MIN_POOL there is no meaningful sigma → (None,...) so the caller uses the
    # absolute-threshold fallback (with a loud warning at the call site).
    tiers, mu, sd = compute_pool_ztiers([200, 150, 100], pool_size=12, position="TE")
    assert tiers is None and mu is None and sd is None


def test_zero_sigma_falls_back():
    tiers, _, _ = compute_pool_ztiers([100, 100, 100, 100, 100, 100], pool_size=6, position="RB")
    assert tiers is None


def test_z_to_tier_monotonic():
    assert z_to_tier(2.0) == 1
    assert z_to_tier(0.5) == 2
    assert z_to_tier(0.0) == 3
    assert z_to_tier(-0.5) == 4
    assert z_to_tier(-2.0) == 5


def test_tiers_are_monotonic_by_points():
    # A higher-projected player is never assigned a worse tier than a lower one.
    pts = sorted([300, 250, 240, 210, 205, 190, 180, 150, 120, 100, 90, 80], reverse=True)
    tiers, _, _ = compute_pool_ztiers(pts, 12, "WR")
    assert tiers == sorted(tiers)  # non-decreasing as points decrease


def test_kdef_never_reaches_ztier_path():
    # K/DEF are valued by value_kdef (forced T5) and never enter the skill pools, so the
    # z-tier machinery must not touch them. assign_tier fallback also has no K/DEF entry —
    # confirm value_kdef stamps T5 regardless.
    class _P:
        position = "K"; name = "Kicker"; injury_profile = None
        baseline_value = ceiling_value = floor_value = None
        tier = None
        recommended_bid_ceiling = let_go_threshold = elite_anchor_weight = None
        risk_adjusted_value = positional_scarcity_modifier = None
        value_gap = value_gap_signal = data_confidence = None
        ai_bid_ceiling = ai_confidence_floor = ai_confidence_ceiling = None
        value_assessment = auction_note = None
        pay_up_flag = nomination_target_flag = False
    p = _P()
    V.value_kdef(p)
    assert p.tier == V._KDEF_TIER == 5
