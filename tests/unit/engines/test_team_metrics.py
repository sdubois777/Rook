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
    compute_base_personnel,
    compute_pass_rates,
    compute_qb_metrics,
    compute_red_zone_philosophy,
    compute_run_block_stuff_rate,
    pass_pro_grade_from_sack_rate,
    qb_tier_from_cpoe,
    run_block_grade_from_stuff_rate,
    scheme_from_pass_rate,
)


# ---------------------------------------------------------------------------
# scheme thresholds — distributes across all three
# ---------------------------------------------------------------------------
def test_scheme_distributes_across_all_three():
    assert scheme_from_pass_rate(0.487) == "run_heavy"    # BAL — the founder's case
    assert scheme_from_pass_rate(0.55) == "balanced"
    assert scheme_from_pass_rate(0.645) == "pass_heavy"   # CIN
    assert scheme_from_pass_rate(None) is None


# ---------------------------------------------------------------------------
# pass-protection — monotonic by sack_rate (the mis-order fix)
# ---------------------------------------------------------------------------
def test_pass_pro_orders_monotonically_by_sack_rate():
    den = pass_pro_grade_from_sack_rate(0.0346)   # best
    bal = pass_pro_grade_from_sack_rate(0.0875)   # near-worst
    lv = pass_pro_grade_from_sack_rate(0.0992)    # worst
    order = {"B+": 3, "B": 4, "B-": 5, "C+": 6, "C": 7, "C-": 8, "D+": 9}
    # DEN's best sack_rate MUST grade better than BAL's (the exact bug this fixes).
    assert order[den] < order[bal] < order[lv]
    assert pass_pro_grade_from_sack_rate(None) is None


def test_pass_pro_grade_is_monotonic_across_the_range():
    rates = [0.03, 0.05, 0.07, 0.09, 0.11]
    order = {"A+": 0, "A": 1, "A-": 2, "B+": 3, "B": 4, "B-": 5, "C+": 6, "C": 7, "C-": 8, "D+": 9}
    grades = [order[pass_pro_grade_from_sack_rate(r)] for r in rates]
    assert grades == sorted(grades)               # worse sack_rate → worse (higher) grade


# ---------------------------------------------------------------------------
# qb_tier — discriminates by cpoe, rookies excepted
# ---------------------------------------------------------------------------
def test_qb_tier_discriminates_by_cpoe():
    assert qb_tier_from_cpoe(3.09) == "elite"     # PHI
    assert qb_tier_from_cpoe(1.0) == "solid"
    assert qb_tier_from_cpoe(-0.28) == "average"  # CIN
    assert qb_tier_from_cpoe(-2.23) == "weak"     # BAL
    assert qb_tier_from_cpoe(None) is None


def test_rookie_qb_keeps_rookie_tier():
    assert qb_tier_from_cpoe(9.0, is_rookie=True) == "rookie"   # no reliable prior data


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


async def test_pass_overwrites_three_fields_and_real_cpoe():
    den = _TS("DEN", 0.0346)
    bal = _TS("BAL", 0.0875)
    rk = _TS("CHI", 0.06, rookie=True)
    db = _FakeDB([den, bal, rk])
    pbp = pd.DataFrame(
        [{"season_type": "REG", "posteam": "DEN", "play_type": "run"}] * 6
        + [{"season_type": "REG", "posteam": "DEN", "play_type": "pass"}] * 4      # 0.40 → run_heavy
        + [{"season_type": "REG", "posteam": "BAL", "play_type": "pass"}] * 13
        + [{"season_type": "REG", "posteam": "BAL", "play_type": "run"}] * 7       # 0.65 → pass_heavy
        + [{"season_type": "REG", "posteam": "CHI", "play_type": "pass"}] * 5
        + [{"season_type": "REG", "posteam": "CHI", "play_type": "run"}] * 5,      # 0.50 → run_heavy
    )
    ngs = pd.DataFrame([
        {"week": 0, "team_abbr": "DEN", "attempts": 500, "completion_percentage_above_expectation": 3.0, "avg_intended_air_yards": 8.0},
        {"week": 0, "team_abbr": "BAL", "attempts": 500, "completion_percentage_above_expectation": -2.23, "avg_intended_air_yards": 9.0},
        {"week": 0, "team_abbr": "CHI", "attempts": 400, "completion_percentage_above_expectation": 5.0, "avg_intended_air_yards": 7.0},
    ])
    res = await apply_team_deterministic_fields(db, stats_season=2025, pbp=pbp, ngs_passing=ngs)
    assert res["teams"] == 3 and res["scheme"] == 3 and res["pass_pro"] == 3

    # scheme from real pass rate
    assert den.oc_scheme == "run_heavy" and float(den.oc_run_pass_split_tendency) == pytest.approx(0.40)
    assert bal.oc_scheme == "pass_heavy"
    # pass-pro ordered: DEN (best sack) beats BAL
    order = {"B+": 3, "C-": 8}
    assert order[den.pass_protection_grade] < order[bal.pass_protection_grade]
    # qb_tier from real cpoe; garbage cpoe replaced with real
    assert den.qb_tier == "elite" and float(den.qb_cpoe) == pytest.approx(3.0)
    assert bal.qb_tier == "weak"
    # rookie keeps rookie tier despite a high cpoe
    assert rk.qb_tier == "rookie"


# ---------------------------------------------------------------------------
# SLICE 2 — run-block (stuff rate), personnel, red-zone
# ---------------------------------------------------------------------------
def test_run_block_grade_monotonic_by_stuff_rate():
    order = {"B+": 3, "B": 4, "B-": 5, "C+": 6, "C": 7, "C-": 8, "D+": 9}
    grades = [order[run_block_grade_from_stuff_rate(r)] for r in (0.049, 0.07, 0.09, 0.13)]
    assert grades == sorted(grades)                 # lower stuff (better OL) → better grade
    # DAL's best stuff_rate must grade better than LV's worst.
    assert order[run_block_grade_from_stuff_rate(0.049)] < order[run_block_grade_from_stuff_rate(0.151)]
    assert run_block_grade_from_stuff_rate(None) is None


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
        [{"season_type": "REG", "posteam": "LV", "play_type": "run", "tackled_for_loss": 1.0,
          "offense_personnel": "1 RB, 2 TE, 2 WR", "yardline_100": 5, "receiver_position": None}] * 6
        + [{"season_type": "REG", "posteam": "LV", "play_type": "pass", "tackled_for_loss": 0.0,
            "offense_personnel": "1 RB, 2 TE, 2 WR", "yardline_100": 5, "receiver_position": "WR"}] * 4
    )
    res = await apply_team_deterministic_fields(db, stats_season=2025, pbp=pbp,
                                                ngs_passing=pd.DataFrame(columns=["week", "team_abbr", "attempts"]))
    assert res["run_block"] == 1 and res["personnel"] == 1 and res["red_zone"] == 1
    assert lv.run_block_stuff_rate is not None                       # real numeric stored
    assert lv.run_blocking_grade == run_block_grade_from_stuff_rate(0.6)  # 6/10 runs stuffed
    assert lv.personnel_tendency == "12"                            # real base (was "11")
    assert lv.red_zone_philosophy == "rb"                          # 60% RZ runs (was "wr1")


async def test_pass_loud_warns_missing_numeric(caplog):
    ghost = _TS("XYZ", 0.06)          # not in pbp/ngs
    db = _FakeDB([ghost])
    with caplog.at_level("WARNING"):
        res = await apply_team_deterministic_fields(db, stats_season=2025, pbp=pd.DataFrame(columns=["season_type","posteam","play_type"]), ngs_passing=pd.DataFrame(columns=["week","team_abbr","attempts"]))
    assert "XYZ" in res["missing_pass_rate"] and "XYZ" in res["missing_cpoe"]
    assert any("missing real pass_rate" in m for m in caplog.messages)
