"""The single scoring definition + per-format reprice math.

Includes deterministic gate evidence for Phase 1:
  G1 — a reception-dependent player's value is materially LOWER in Standard.
  G3 — a pure rusher's value barely moves across formats (no spurious repricing).
Both computed with the REAL valuation PAR functions, so this proves the scoring
LAYER is format-aware without needing a full pipeline run.
"""
from __future__ import annotations

import pytest

from backend import scoring
from backend.engines.valuation import calculate_replacement_level, ppr_to_system_value
from backend.models.league_config import LeagueConfig


# --- the one definition -----------------------------------------------------
def test_rec_points_all_formats_incl_standard():
    assert scoring.rec_points("ppr") == 1.0
    assert scoring.rec_points("half_ppr") == 0.5
    assert scoring.rec_points("standard") == 0.0   # the bug that made standard == PPR
    assert scoring.rec_points(None) == 1.0          # safe default (disclosed at read)
    assert scoring.rec_points("weird") == 1.0


def test_season_points_reprice_exact():
    # A pass-catcher: 200 PPR incl. 80 receptions.
    assert scoring.season_points(200, 80, "ppr") == 200.0
    assert scoring.season_points(200, 80, "half_ppr") == 160.0   # −0.5×80
    assert scoring.season_points(200, 80, "standard") == 120.0   # −1.0×80
    # A non-receiver (QB / unknown receptions) never moves.
    assert scoring.season_points(300, 0, "standard") == 300.0
    assert scoring.season_points(300, None, "standard") == 300.0
    # Never goes negative.
    assert scoring.season_points(10, 40, "standard") == 0.0


def test_nearest_preset_maps_custom():
    assert scoring.nearest_preset(0.9) == "ppr"
    assert scoring.nearest_preset(0.6) == "half_ppr"
    assert scoring.nearest_preset(0.1) == "standard"
    assert scoring.nearest_preset(None) == "ppr"


def test_league_config_rec_points_delegates_and_fixes_standard():
    assert LeagueConfig(scoring="ppr").rec_points == 1.0
    assert LeagueConfig(scoring="half_ppr").rec_points == 0.5
    # Previously returned 1.0 (silently PPR) — now correct.
    assert LeagueConfig(scoring="standard").rec_points == 0.0


# --- gate evidence: reprice a real position pool ----------------------------
# (name, ppr_points, receptions) — a pool of RBs mixing pass-catchers and rushers
# at matched PPR totals, so replacement level is meaningful.
_RB_POOL = [
    ("PassCatch1", 280, 80), ("PassCatch2", 250, 72), ("PassCatch3", 215, 64),
    ("Rusher1", 280, 22), ("Rusher2", 250, 18), ("Rusher3", 215, 20),
    ("Fill1", 185, 40), ("Fill2", 165, 30), ("Fill3", 150, 34),
    ("Fill4", 130, 22), ("Fill5", 110, 16), ("Fill6", 95, 12),
]


def _value_by_format(fmt: str) -> dict[str, float]:
    """Reprice the RB pool into `fmt` and return each player's system value using
    the production PAR math (position-relative, replacement-aware)."""
    pts = [(n, scoring.season_points(ppr, rec, fmt)) for n, ppr, rec in _RB_POOL]
    ppr_list = sorted((p for _, p in pts), reverse=True)
    repl = calculate_replacement_level(ppr_list, len(ppr_list))
    total_par = sum(max(0.0, p - repl) for p in ppr_list) or 1.0
    return {
        n: float(ppr_to_system_value(p, repl, total_par, position_budget=100.0))
        for n, p in pts
    }


def test_G1_reception_dependent_value_drops_materially_in_standard():
    ppr_v = _value_by_format("ppr")
    half_v = _value_by_format("half_ppr")
    std_v = _value_by_format("standard")
    pc = "PassCatch1"
    # Monotonic: PPR >= Half >= Standard, and Standard MATERIALLY lower.
    assert ppr_v[pc] > half_v[pc] > std_v[pc], (ppr_v[pc], half_v[pc], std_v[pc])
    drop = (ppr_v[pc] - std_v[pc]) / ppr_v[pc]
    assert drop > 0.15, f"pass-catcher only dropped {drop:.0%} PPR→STD"


def test_G3_pure_rusher_is_not_repriced_down():
    """A pure rusher is format-neutral: his POINTS barely move (intrinsic — receptions
    are a tiny slice), and his VALUE is NOT dragged down like a pass-catcher's. In a
    position-relative system his value may even RISE in Standard (pass-catchers fell
    below him) — the one thing it must not do is drop. Proves we reprice by receptions,
    not spuriously move everyone."""
    ppr_pts = scoring.season_points(280, 22, "ppr")        # 280
    std_pts = scoring.season_points(280, 22, "standard")   # 258
    assert (ppr_pts - std_pts) / ppr_pts < 0.10            # ~8% intrinsic move

    ppr_v = _value_by_format("ppr")
    std_v = _value_by_format("standard")
    assert std_v["Rusher1"] >= ppr_v["Rusher1"] * 0.98     # value not dragged down


def test_G1_relative_pass_catcher_drops_more_than_rusher():
    """At equal PPR totals, the pass-catcher must lose more value than the rusher
    going to Standard — the core correctness the whole build exists to deliver."""
    ppr_v = _value_by_format("ppr")
    std_v = _value_by_format("standard")
    pc_drop = ppr_v["PassCatch1"] - std_v["PassCatch1"]
    ru_drop = ppr_v["Rusher1"] - std_v["Rusher1"]
    assert pc_drop > ru_drop, (pc_drop, ru_drop)
