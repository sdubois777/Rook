"""
Unit tests for the dedicated defense (DST) preseason prior
(services/defense_baseline.py).

Covers the pure per-season compute (DEF-only), the write path's source selection
(historical / no-history default + loud-warn), and the payoffs: a defense with a
prior now produces a sane PRIOR-DOMINATED forward_ppg early-season, the in-season
DST matchup tilt still rides ON TOP of that blended base, and the prior stays
invisible late-season.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.defense_baseline import (
    DEF_DEFAULT_PPG,
    _default_total,
    compute_defense_season_ppg,
    write_defense_baselines,
)
from backend.services.kicker_baseline import GAMES_BASIS
from backend.services.kdef_matchup import apply_dst_tilt
from backend.services.trade.value_engine import compute_player_value


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _scored(rows):
    """A scored weekly K/DST frame (kdef_scoring.weekly_kdef_value_frame shape):
    rows = (canonical_player_id, position, week, fantasy_points_ppr)."""
    return pd.DataFrame(
        [{"canonical_player_id": cid, "position": pos, "week": wk, "fantasy_points_ppr": pts}
         for cid, pos, wk, pts in rows]
    )


def _def_weeks(ppgs):
    """A per-week DEF frame for compute_player_value (zero usage — DST has no
    snaps/targets)."""
    return pd.DataFrame(
        [{"week": i + 1, "snap_pct": 0.0, "target_share": 0.0,
          "fantasy_points_ppr": p, "targets": 0, "carries": 0}
         for i, p in enumerate(ppgs)]
    )


class _Res:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDB:
    """FIRST execute (the Player-DEF select) → the DEF rows; every subsequent
    execute (per-DEF profile lookup) → None → a fresh profile, captured via add()."""
    def __init__(self, drows):
        self._drows = drows
        self._n = 0
        self.added = []

    async def execute(self, *_a, **_k):
        self._n += 1
        return _Res(rows=self._drows) if self._n == 1 else _Res(scalar=None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


# ---------------------------------------------------------------------------
# pure compute
# ---------------------------------------------------------------------------
def test_compute_defense_season_ppg_is_per_game_and_def_only():
    frames = {
        2024: _scored([
            ("den", "DEF", 1, 12.0), ("den", "DEF", 2, 6.0),   # 2 games → 9.0
            ("k1", "K", 1, 8.0),                                # K ignored
        ]),
        2023: _scored([("den", "DEF", 1, 7.0), ("den", "DEF", 2, 7.0), ("den", "DEF", 3, 7.0)]),
    }
    out = compute_defense_season_ppg(frames)
    assert out["den"][2024] == pytest.approx(9.0)
    assert out["den"][2023] == pytest.approx(7.0)
    assert "k1" not in out                                     # kickers excluded


def test_default_total_is_in_a_sane_dst_range():
    # ÷17 recovers the league-average DST ppg fallback (sane band ~5-10).
    assert 5.0 <= DEF_DEFAULT_PPG <= 10.0
    assert _default_total() / GAMES_BASIS == pytest.approx(DEF_DEFAULT_PPG)


# ---------------------------------------------------------------------------
# write path — source selection
# ---------------------------------------------------------------------------
async def test_write_historical_defense_uses_weighted_total():
    # recency weights (0.5, 0.3) over 2024=8.0, 2023=6.0 → (8*0.5+6*0.3)/0.8 = 7.25
    frames = {
        2024: _scored([("den", "DEF", 1, 8.0), ("den", "DEF", 2, 8.0)]),
        2023: _scored([("den", "DEF", 1, 6.0), ("den", "DEF", 2, 6.0)]),
    }
    db = _FakeDB([("den", "Denver Broncos")])
    res = await write_defense_baselines(db, scored_by_season=frames, seasons=[2024, 2023, 2022])
    assert res["historical"] == 1 and res["default_used"] == 0
    prof = db.added[0]
    assert prof.profile_source == "defense_history"
    assert prof.is_rookie is False                             # team units aren't rookies
    assert prof.clean_season_baseline["ppr_points"] == pytest.approx(round(7.25 * GAMES_BASIS, 1))


async def test_write_no_history_defense_defaults_and_loud_warns(caplog):
    db = _FakeDB([("mystery", "Unresolvable DEF")])            # no scored rows
    with caplog.at_level("WARNING"):
        res = await write_defense_baselines(db, scored_by_season={2024: _scored([])}, seasons=[2024])
    assert res["default_used"] == 1 and res["historical"] == 0
    prof = db.added[0]
    assert prof.profile_source == "defense_default"
    assert prof.clean_season_baseline["ppr_points"] == pytest.approx(_default_total())
    assert any("no DST scoring" in r.message for r in caplog.records)   # loud-warn, not silent


# ---------------------------------------------------------------------------
# the payoffs — early-season prior-dominated, tilt-on-top, late-season invisible
# ---------------------------------------------------------------------------
def test_defense_prior_dominates_early_season_instead_of_zero():
    """A defense with a prior but no games yet (Week 1/2) produces a sane value
    instead of the 0/garbage a null prior gave."""
    prior = 143.4 / 17.0   # ≈ 8.44, Denver's real historical season-total ÷17
    no_games = pd.DataFrame(columns=["week", "snap_pct", "target_share",
                                     "fantasy_points_ppr", "targets", "carries"])
    v = compute_player_value(
        canonical_player_id="den", name="Denver Broncos", position="DEF",
        weeks=no_games, current_week=2, prior_projection_ppg=prior,
    )
    assert v.forward_ppg == pytest.approx(round(prior, 2))     # prior-only at 0 games
    assert v.forward_value > 0.0                               # NOT 0/garbage


def test_dst_tilt_rides_on_top_of_the_blended_prior_base():
    """#3 — the in-season DST matchup tilt composes ON TOP of the blended prior base
    (a real base + tilt), not a near-zero base + tilt as before the prior existed."""
    # Week-2 blended base: 2 modest games + a strong prior → prior-dominated, real.
    base = compute_player_value(
        canonical_player_id="den", name="Denver Broncos", position="DEF",
        weeks=_def_weeks([5.0, 5.0]), current_week=2, prior_projection_ppg=8.44,
    )
    assert base.prior_weight == pytest.approx(0.6, abs=0.01)   # prior-dominated at 2 games
    assert base.forward_ppg > 3.0                              # a REAL base, not near-zero

    # A sack/turnover-prone opponent (IND) vs a clean one (SF) → positive DEN tilt.
    signal = {
        "IND": {"sacks_allowed_pg": 3.5, "giveaways_pg": 1.8, "points_pg": 16.0},
        "SF": {"sacks_allowed_pg": 1.0, "giveaways_pg": 0.4, "points_pg": 28.0},
    }
    out, ctx = apply_dst_tilt(
        {"den": base}, {"den": "DEN"}, signal, {"DEN": "IND"}, week=2,
    )
    tilt = ctx["den"]["tilt"]
    assert tilt > 0                                            # favorable matchup lifts DEN
    # The tilt is ADDED to the blended base — composition, not replacement.
    assert out["den"].forward_ppg == pytest.approx(round(base.forward_ppg + tilt, 2))


def test_defense_prior_invisible_late_season():
    """At ≥5 played games prior_weight is 0 → the defense's value is identical with or
    without the new prior (week-14 invisibility)."""
    weeks = _def_weeks([7.0] * 6)                              # 6 played games
    common = dict(canonical_player_id="den", name="Denver Broncos", position="DEF",
                  weeks=weeks, current_week=14)
    with_prior = compute_player_value(**common, prior_projection_ppg=200.0 / 17.0)
    no_prior = compute_player_value(**common, prior_projection_ppg=None)
    assert with_prior.prior_weight == 0.0
    assert with_prior.forward_ppg == pytest.approx(no_prior.forward_ppg)
