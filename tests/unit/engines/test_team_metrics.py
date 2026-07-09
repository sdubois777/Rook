"""
Tests for the deterministic Teams-page fields (engines/team_metrics.py, slice 1):
scheme distributes across all three by real pass_rate, pass-protection orders
monotonically by sack_rate, qb_tier discriminates by real cpoe. The async pass uses
injected PBP/NGS frames + a fake session (no fetch, no DB).
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backend.engines.team_metrics import (
    apply_team_deterministic_fields,
    bell_rank,
    compute_base_personnel,
    compute_pass_rates,
    compute_qb_metrics,
    compute_qb_value,
    compute_red_zone_philosophy,
    compute_run_block_stuff_rate,
    grade_from_pct,
    qb_value_pct,
    scheme_from_pass_rate,
    system_composite_pct,
    tier_from_pct,
)


# ---------------------------------------------------------------------------
# ACCURACY FIX — QB sub-composite (EPA + success + fantasy PPG incl. RUSHING)
# ---------------------------------------------------------------------------
def test_compute_qb_value_includes_rushing_production():
    # A mobile QB (rushing yards + TDs) must score higher fppg than his passing alone —
    # the exact thing cpoe misses. Two QBs, same passing, one adds rushing.
    base = {"season_type": "REG", "qb_dropback": 1, "passing_yards": 20.0, "pass_touchdown": 0.0,
            "interception": 0.0, "rushing_yards": 0.0, "rush_touchdown": 0.0, "fumble_lost": 0.0,
            "qb_epa": 0.1, "success": 1.0, "game_id": "g1"}
    rows = []
    for g in range(6):
        rows.append({**base, "posteam": "POC", "passer_player_name": "Pocket QB", "rusher_player_name": None, "game_id": f"p{g}"})
        # mobile QB: same passing + 40 rush yds & a rush TD per game
        rows.append({**base, "posteam": "MOB", "passer_player_name": "Mobile QB", "rusher_player_name": None, "game_id": f"m{g}"})
        rows.append({"season_type": "REG", "qb_dropback": 0, "play_type": "run", "posteam": "MOB",
                     "passer_player_name": None, "rusher_player_name": "Mobile QB", "rushing_yards": 40.0,
                     "rush_touchdown": 1.0, "passing_yards": 0.0, "pass_touchdown": 0.0, "interception": 0.0,
                     "fumble_lost": 0.0, "qb_epa": 0.0, "success": 0.0, "game_id": f"mr{g}"})
    v = compute_qb_value(pd.DataFrame(rows))
    assert "MOB" in v and "POC" in v
    # fantasy PPG (index 2): mobile QB is much higher purely from rushing.
    assert v["MOB"][2] > v["POC"][2] + 5.0
    # rush_fppg (index 3, the de-confound input): mobile QB ~10/g (40yd*.1 + 6 TD), pocket 0.
    assert v["MOB"][3] > 5.0
    assert v["POC"][3] == pytest.approx(0.0)


def test_qb_value_pct_blend_weights_and_missing():
    # EPA (0.45) heaviest, then fppg (0.35), then success (0.20).
    full = qb_value_pct(epa_pct=1.0, fppg_pct=0.0, success_pct=0.0)
    assert full == pytest.approx(0.45)
    assert qb_value_pct(epa_pct=0.8, fppg_pct=None, success_pct=None) == pytest.approx(0.8)  # renormalise
    assert qb_value_pct(None, None, None) is None


def test_qb_value_pct_rushing_bonus_is_additive_and_spares_pockets():
    """The rushing de-confound: additive on rush_pct, never penalises non-rushers, clamped."""
    base = qb_value_pct(0.5, 0.5, 0.5)                       # 0.50
    # a pocket passer (rush_pct 0) does NOT move
    assert qb_value_pct(0.5, 0.5, 0.5, rush_pct=0.0) == pytest.approx(base)
    # a top rusher gets the full +0.15 bonus (0.15 * 1.0)
    assert qb_value_pct(0.5, 0.5, 0.5, rush_pct=1.0) == pytest.approx(base + 0.15)
    # bonus scales with rushing production percentile
    assert qb_value_pct(0.5, 0.5, 0.5, rush_pct=0.5) == pytest.approx(base + 0.075)
    # clamped at 1.0 (an elite dual-threat can't exceed the ceiling)
    assert qb_value_pct(1.0, 1.0, 1.0, rush_pct=1.0) == pytest.approx(1.0)
    # None ⇒ backward-compatible (no bonus)
    assert qb_value_pct(0.5, 0.5, 0.5, rush_pct=None) == pytest.approx(base)


# ---------------------------------------------------------------------------
# scheme thresholds — distributes across all three
# ---------------------------------------------------------------------------
def test_scheme_distributes_across_all_three():
    assert scheme_from_pass_rate(0.487) == "run_heavy"    # BAL — the founder's case
    assert scheme_from_pass_rate(0.55) == "balanced"
    assert scheme_from_pass_rate(0.645) == "pass_heavy"   # CIN
    assert scheme_from_pass_rate(None) is None


# ---------------------------------------------------------------------------
# SLICE 3 — the widened-bell mapper (one curve, full A–F, dense middle)
# ---------------------------------------------------------------------------
def _bell_over_32(lower_is_better):
    """Distribution of grades when 32 evenly-spaced values run through the bell."""
    import collections
    vals = [i / 100.0 for i in range(32)]          # 32 distinct values
    return collections.Counter(
        grade_from_pct(bell_rank(v, vals, lower_is_better=lower_is_better)) for v in vals
    )


def test_bell_distribution_is_widened_dense_middle():
    # Target ≈ 3/6/13/6/3 A/B/C/D/F over 32 teams — tails populated, dense C middle.
    dist = _bell_over_32(lower_is_better=False)
    assert dist["A"] == 4 and dist["F"] == 4        # tails populated (not one lone A)
    assert dist["C"] >= 10                          # dense middle (C = the average)
    assert set(dist) == {"A", "B", "C", "D", "F"}   # full scale used
    assert sum(dist.values()) == 32


def test_bell_ordering_best_gets_A_worst_gets_F():
    vals = [0.03, 0.05, 0.07, 0.09, 0.11, 0.13]     # lower = better OL
    assert grade_from_pct(bell_rank(0.03, vals, lower_is_better=True)) == "A"   # best → A
    assert grade_from_pct(bell_rank(0.13, vals, lower_is_better=True)) == "F"   # worst → F


def test_bell_direction_higher_is_better_for_cpoe():
    vals = [-5.0, 0.0, 5.0]
    assert grade_from_pct(bell_rank(5.0, vals, lower_is_better=False)) == "A"   # best cpoe → A
    assert grade_from_pct(bell_rank(-5.0, vals, lower_is_better=False)) == "F"  # worst → F
    assert bell_rank(None, vals, lower_is_better=False) is None
    assert bell_rank(1.0, [], lower_is_better=False) is None


def test_tier_from_pct_and_rookie_exception():
    assert tier_from_pct(0.95) == "elite"
    assert tier_from_pct(0.80) == "solid"
    assert tier_from_pct(0.50) == "average"
    assert tier_from_pct(0.05) == "weak"
    assert tier_from_pct(0.99, is_rookie=True) == "rookie"   # rookies excepted from the bell


def test_system_composite_is_qb_dominant():
    # QB is now the heaviest component (0.55) — the reweight fix. A missing component
    # renormalises, never fabricated.
    full = system_composite_pct(pass_pct=0.5, run_pct=0.5, qb_pct=1.0)
    assert full == pytest.approx(0.55 * 1.0 + 0.25 * 0.5 + 0.20 * 0.5)
    assert system_composite_pct(0.8, None, None) == pytest.approx(0.8)   # single comp
    assert system_composite_pct(None, None, None) is None


def test_qb_dominates_line_in_composite():
    # An elite-QB / bad-line team beats a bad-QB / elite-line team — football is
    # QB-driven (the KC-above-CHI intent: QB weight dominates line strength).
    elite_qb = system_composite_pct(pass_pct=0.2, run_pct=0.2, qb_pct=1.0)
    elite_line = system_composite_pct(pass_pct=1.0, run_pct=1.0, qb_pct=0.2)
    assert elite_qb > elite_line


# ---------------------------------------------------------------------------
# real-stat computation over injected frames
# ---------------------------------------------------------------------------
def test_compute_pass_rates_from_pbp():
    pbp = pd.DataFrame([
        {"season_type": "REG", "posteam": "CIN", "play_type": "pass"},
        {"season_type": "REG", "posteam": "CIN", "play_type": "pass"},
        {"season_type": "REG", "posteam": "CIN", "play_type": "run"},
        {"season_type": "REG", "posteam": "BAL", "play_type": "run"},
        {"season_type": "REG", "posteam": "BAL", "play_type": "run"},
        {"season_type": "REG", "posteam": "BAL", "play_type": "pass"},
        {"season_type": "POST", "posteam": "BAL", "play_type": "pass"},   # excluded
    ])
    pr = compute_pass_rates(pbp)
    assert pr["CIN"] == pytest.approx(0.6667, abs=0.001)
    assert pr["BAL"] == pytest.approx(0.3333, abs=0.001)


def test_compute_qb_metrics_primary_passer_and_rams_alias():
    ngs = pd.DataFrame([
        {"week": 0, "team_abbr": "PHI", "attempts": 500, "completion_percentage_above_expectation": 3.09, "avg_intended_air_yards": 9.0},
        {"week": 0, "team_abbr": "PHI", "attempts": 20, "completion_percentage_above_expectation": -5.0, "avg_intended_air_yards": 5.0},  # backup, ignored
        {"week": 0, "team_abbr": "LAR", "attempts": 480, "completion_percentage_above_expectation": 1.2, "avg_intended_air_yards": 8.0},   # Rams → LA alias
    ])
    m = compute_qb_metrics(ngs)
    assert m["PHI"] == (3.09, 9.0)      # primary passer only
    assert m["LA"] == (1.2, 8.0)        # LAR aliased to Rook's LA


# ---------------------------------------------------------------------------
# the async pass — overwrites the 3 fields, loud-warns missing
# ---------------------------------------------------------------------------
class _TS:
    def __init__(self, team, sack_rate, rookie=False, season=2026):
        self.team_abbr = team
        self.season_year = season
        self.sack_rate = Decimal(str(sack_rate)) if sack_rate is not None else None
        self.rookie_qb_flag = rookie
        self.oc_scheme = "balanced"
        self.oc_run_pass_split_tendency = Decimal("0.45")
        self.pass_protection_grade = "B"
        self.qb_tier = "solid"
        self.qb_cpoe = Decimal("62.5")            # LLM garbage, should be overwritten
        self.qb_air_yards_per_attempt = Decimal("0")


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0

    async def execute(self, *_a, **_k):
        return type("R", (), {"scalars": lambda _s: _Scalars(self._rows)})()

    async def commit(self):
        self.commits += 1


async def test_apply_sources_season_from_the_latest_data_resolver(monkeypatch):
    """Provenance: when no season is passed, the season comes from the ONE resolver
    (latest_season_with_data) — the SAME one team_systems uses — so QB value, scheme,
    run-block and pass-pro all read a single, data-driven season. No field improvises
    get_current_season()-1 anymore."""
    hit = {}

    def spy():
        hit["called"] = True
        return 2025

    monkeypatch.setattr("backend.engines.team_metrics.latest_season_with_data", spy)
    empty_pbp = pd.DataFrame(columns=["season_type", "posteam", "play_type"])
    empty_ngs = pd.DataFrame(columns=["week", "team_abbr", "attempts"])
    # pbp/ngs injected → no fetch; stats_season unset → must invoke the resolver
    await apply_team_deterministic_fields(_FakeDB([]), pbp=empty_pbp, ngs_passing=empty_ngs)
    assert hit.get("called") is True


def _pass_play(team, passer, epa, succ, yds=25.0, td=0.0, intc=0.0):
    return {"season_type": "REG", "posteam": team, "play_type": "pass", "qb_dropback": 1,
            "passer_player_name": passer, "rusher_player_name": None, "qb_epa": epa, "success": succ,
            "passing_yards": yds, "pass_touchdown": td, "interception": intc,
            "rushing_yards": 0.0, "rush_touchdown": 0.0, "fumble_lost": 0.0}


def _run_play(team):
    return {"season_type": "REG", "posteam": team, "play_type": "run", "qb_dropback": 0,
            "passer_player_name": None, "rusher_player_name": None, "qb_epa": 0.0, "success": 0.0,
            "passing_yards": 0.0, "pass_touchdown": 0.0, "interception": 0.0,
            "rushing_yards": 0.0, "rush_touchdown": 0.0, "fumble_lost": 0.0}


async def test_pass_overwrites_qb_tier_and_scheme_from_real_stats():
    den = _TS("DEN", 0.0346)         # elite QB (high EPA)
    bal = _TS("BAL", 0.0875)         # weak QB (low EPA)
    rk = _TS("CHI", 0.06, rookie=True)
    db = _FakeDB([den, bal, rk])
    # ≥6 games each so compute_qb_value keeps them; DEN high EPA/success, BAL low.
    # DEN wins all 3 QB-value axes: high EPA + success AND high per-pass production
    # (big yards + TDs) so fppg beats BAL despite fewer attempts; BAL throws picks.
    rows = []
    for g in range(6):
        rows += [_pass_play("DEN", "Elite QB", 0.4, 1.0, yds=40.0, td=0.5)] * 4 + [dict(_run_play("DEN"), game_id=f"d{g}")] * 6
        rows += [_pass_play("BAL", "Weak QB", -0.4, 0.0, yds=15.0, intc=0.15)] * 13 + [dict(_run_play("BAL"), game_id=f"b{g}")] * 7
        rows += [_pass_play("CHI", "Rookie QB", 0.1, 0.5)] * 5 + [dict(_run_play("CHI"), game_id=f"c{g}")] * 5
    pbp = pd.DataFrame(rows)
    pbp["game_id"] = [f"{r['posteam']}{i//10}" for i, r in enumerate(rows)]
    ngs = pd.DataFrame([
        {"week": 0, "team_abbr": "DEN", "attempts": 500, "completion_percentage_above_expectation": 3.0, "avg_intended_air_yards": 8.0},
        {"week": 0, "team_abbr": "BAL", "attempts": 500, "completion_percentage_above_expectation": -2.23, "avg_intended_air_yards": 9.0},
        {"week": 0, "team_abbr": "CHI", "attempts": 400, "completion_percentage_above_expectation": 5.0, "avg_intended_air_yards": 7.0},
    ])
    res = await apply_team_deterministic_fields(db, stats_season=2025, pbp=pbp, ngs_passing=ngs)
    assert res["teams"] == 3 and res["scheme"] == 3 and res["pass_pro"] == 3

    # scheme from real pass rate
    assert den.oc_scheme == "run_heavy" and bal.oc_scheme == "pass_heavy"
    # pass-pro on the widened bell: DEN (best sack) → A, BAL (worst) → F
    assert den.pass_protection_grade == "A" and bal.pass_protection_grade == "F"
    # qb_tier from the EPA-weighted SUB-COMPOSITE (not cpoe): DEN elite, BAL weak
    assert den.qb_tier == "elite" and bal.qb_tier == "weak"
    assert rk.qb_tier == "rookie"           # rookie excepted
    assert float(den.qb_cpoe) == pytest.approx(3.0)   # cpoe still stored for display


# ---------------------------------------------------------------------------
# SLICE 2 — run-block (stuff rate), personnel, red-zone
# ---------------------------------------------------------------------------
def test_run_block_bell_orders_by_stuff_rate():
    vals = [0.049, 0.07, 0.09, 0.13, 0.151]         # lower stuff = better OL
    # DAL's best stuff_rate → A; LV's worst → F (bell, lower_is_better).
    assert grade_from_pct(bell_rank(0.049, vals, lower_is_better=True)) == "A"
    assert grade_from_pct(bell_rank(0.151, vals, lower_is_better=True)) == "F"


def test_compute_run_block_stuff_rate():
    pbp = pd.DataFrame([
        {"season_type": "REG", "posteam": "LV", "play_type": "run", "tackled_for_loss": 1.0},
        {"season_type": "REG", "posteam": "LV", "play_type": "run", "tackled_for_loss": 0.0},
        {"season_type": "REG", "posteam": "DAL", "play_type": "run", "tackled_for_loss": 0.0},
        {"season_type": "REG", "posteam": "DAL", "play_type": "run", "tackled_for_loss": 0.0},
        {"season_type": "REG", "posteam": "DAL", "play_type": "run", "tackled_for_loss": 1.0},
        {"season_type": "REG", "posteam": "DAL", "play_type": "pass", "tackled_for_loss": 0.0},  # non-run ignored
    ])
    sr = compute_run_block_stuff_rate(pbp)
    assert sr["LV"] == pytest.approx(0.5)
    assert sr["DAL"] == pytest.approx(0.3333, abs=0.001)


def test_compute_base_personnel_shorthand():
    pbp = pd.DataFrame([
        {"season_type": "REG", "posteam": "KC", "offense_personnel": "1 RB, 1 TE, 3 WR"},
        {"season_type": "REG", "posteam": "KC", "offense_personnel": "1 RB, 1 TE, 3 WR"},
        {"season_type": "REG", "posteam": "KC", "offense_personnel": "1 RB, 2 TE, 2 WR"},
        {"season_type": "REG", "posteam": "BAL", "offense_personnel": "1 RB, 2 TE, 2 WR"},
    ])
    p = compute_base_personnel(pbp)
    assert p["KC"] == "11"      # RB=1, TE=1
    assert p["BAL"] == "12"     # RB=1, TE=2


def test_compute_red_zone_philosophy_rb_vs_spread():
    pbp = pd.DataFrame(
        # BAL: run-heavy in the RZ → "rb"
        [{"season_type": "REG", "posteam": "BAL", "yardline_100": 8, "play_type": "run", "receiver_position": None}] * 7
        + [{"season_type": "REG", "posteam": "BAL", "yardline_100": 8, "play_type": "pass", "receiver_position": "WR"}] * 3
        # CIN: pass-leaning, no dominant receiver group (WR share < 0.55, TE < WR) → "spread"
        + [{"season_type": "REG", "posteam": "CIN", "yardline_100": 8, "play_type": "run", "receiver_position": None}] * 3
        + [{"season_type": "REG", "posteam": "CIN", "yardline_100": 8, "play_type": "pass", "receiver_position": "WR"}] * 4
        + [{"season_type": "REG", "posteam": "CIN", "yardline_100": 8, "play_type": "pass", "receiver_position": "RB"}] * 5
    )
    rz = compute_red_zone_philosophy(pbp)
    assert rz["BAL"] == "rb"        # 70% RZ runs
    assert rz["CIN"] == "spread"    # pass-leaning, no dominant receiver group


async def test_pass_writes_slice2_fields(monkeypatch):
    lv = _TS("LV", 0.09)
    lv.run_block_stuff_rate = None
    lv.personnel_tendency = "11"
    lv.red_zone_philosophy = "wr1"
    db = _FakeDB([lv])
    pbp = pd.DataFrame(
        # 6 runs, 3 stuffed → stuff_rate 0.5; 6 runs / 10 plays → RZ run-share 0.6 → "rb"
        [{"season_type": "REG", "posteam": "LV", "play_type": "run", "tackled_for_loss": 1.0,
          "offense_personnel": "1 RB, 2 TE, 2 WR", "yardline_100": 5, "receiver_position": None}] * 3
        + [{"season_type": "REG", "posteam": "LV", "play_type": "run", "tackled_for_loss": 0.0,
            "offense_personnel": "1 RB, 2 TE, 2 WR", "yardline_100": 5, "receiver_position": None}] * 3
        + [{"season_type": "REG", "posteam": "LV", "play_type": "pass", "tackled_for_loss": 0.0,
            "offense_personnel": "1 RB, 2 TE, 2 WR", "yardline_100": 5, "receiver_position": "WR"}] * 4
    )
    res = await apply_team_deterministic_fields(db, stats_season=2025, pbp=pbp,
                                                ngs_passing=pd.DataFrame(columns=["week", "team_abbr", "attempts"]))
    assert res["run_block"] == 1 and res["personnel"] == 1 and res["red_zone"] == 1
    assert lv.run_block_stuff_rate == Decimal("0.5")               # real numeric stored (3/6 stuffed)
    assert lv.personnel_tendency == "12"                          # real base (was "11")
    assert lv.red_zone_philosophy == "rb"                        # 60% RZ runs (was "wr1")
    # (the run_blocking_grade is a widened-bell rank across the league — see
    #  test_run_block_bell_orders_by_stuff_rate; a single-team fixture has no rank.)


async def test_pass_loud_warns_missing_numeric(caplog):
    ghost = _TS("XYZ", 0.06)          # not in pbp/ngs
    db = _FakeDB([ghost])
    with caplog.at_level("WARNING"):
        res = await apply_team_deterministic_fields(db, stats_season=2025, pbp=pd.DataFrame(columns=["season_type","posteam","play_type"]), ngs_passing=pd.DataFrame(columns=["week","team_abbr","attempts"]))
    assert "XYZ" in res["missing_pass_rate"] and "XYZ" in res["missing_cpoe"]
    assert any("missing real pass_rate" in m for m in caplog.messages)
