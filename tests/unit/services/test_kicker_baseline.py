"""
Unit tests for the dedicated kicker preseason prior (services/kicker_baseline.py).

Covers the pure compute (per-season ppg + recency-weighted season total), the
write path's source selection (historical / rookie-default / veteran-default with
a loud-warn), the rookie-K default that closes the double-gap, and the payoff:
a kicker with a prior now produces a sane PRIOR-DOMINATED forward_ppg early-season
(instead of 0/garbage) while staying invisible late-season.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.kicker_baseline import (
    GAMES_BASIS,
    _rookie_default_total,
    compute_kicker_season_ppg,
    weighted_baseline_total,
    write_kicker_baselines,
)
from backend.services.trade.value_engine import compute_player_value


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _scored(rows):
    """A scored weekly K/DEF frame (kdef_scoring.weekly_kdef_value_frame shape):
    rows = (canonical_player_id, position, week, fantasy_points_ppr)."""
    return pd.DataFrame(
        [{"canonical_player_id": cid, "position": pos, "week": wk, "fantasy_points_ppr": pts}
         for cid, pos, wk, pts in rows]
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
    """Async DB double: the FIRST execute (the Player-K select) returns the kicker
    rows; every subsequent execute (per-kicker PlayerProfile lookup) returns None
    → a fresh profile, captured via add()."""
    def __init__(self, krows):
        self._krows = krows
        self._n = 0
        self.added = []

    async def execute(self, *_a, **_k):
        self._n += 1
        return _Res(rows=self._krows) if self._n == 1 else _Res(scalar=None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


# ---------------------------------------------------------------------------
# pure compute
# ---------------------------------------------------------------------------
def test_compute_kicker_season_ppg_is_per_played_game_and_k_only():
    frames = {
        2024: _scored([
            ("k1", "K", 1, 10.0), ("k1", "K", 2, 6.0),        # 2 games → 8.0 ppg
            ("d1", "DEF", 1, 12.0),                            # DEF ignored
        ]),
        2023: _scored([("k1", "K", 1, 9.0), ("k1", "K", 2, 9.0), ("k1", "K", 3, 9.0)]),  # 9.0
    }
    out = compute_kicker_season_ppg(frames)
    assert out["k1"][2024] == pytest.approx(8.0)
    assert out["k1"][2023] == pytest.approx(9.0)
    assert "d1" not in out                                    # DEF excluded


def test_weighted_baseline_total_recency_weights_and_x17():
    # most-recent-first weights (0.5, 0.3): (8.0*0.5 + 9.0*0.3)/0.8 = 8.375 ppg → ×17
    total = weighted_baseline_total({2024: 8.0, 2023: 9.0}, [2024, 2023, 2022])
    assert total == pytest.approx(round(8.375 * GAMES_BASIS, 1))


def test_weighted_baseline_total_single_season_and_empty():
    assert weighted_baseline_total({2024: 8.0}, [2024, 2023]) == pytest.approx(8.0 * GAMES_BASIS)
    assert weighted_baseline_total({}, [2024, 2023]) is None   # no data → None (→ default)


def test_rookie_default_total_matches_convention():
    # season total ÷17 recovers the league-average kicker ppg (7.5).
    assert _rookie_default_total() / GAMES_BASIS == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# write path — source selection
# ---------------------------------------------------------------------------
async def test_write_historical_kicker_uses_weighted_total():
    frames = {2024: _scored([("k1", "K", 1, 8.0), ("k1", "K", 2, 8.0)])}   # 8.0 ppg
    db = _FakeDB([("k1", "Real Kicker", False)])
    res = await write_kicker_baselines(db, scored_by_season=frames, seasons=[2024, 2023, 2022])
    assert res["historical"] == 1 and res["rookie_default"] == 0 and res["vet_default"] == 0
    prof = db.added[0]
    assert prof.profile_source == "kicker_history"
    assert prof.clean_season_baseline["ppr_points"] == pytest.approx(8.0 * GAMES_BASIS)


async def test_write_rookie_kicker_gets_position_default_no_warn(caplog):
    """Rookie K with no history → the position default (double-gap closed), and it's
    an EXPECTED path (no veteran loud-warn)."""
    db = _FakeDB([("rk", "Rookie Kicker", True)])       # is_rookie=True, no scored data
    with caplog.at_level("WARNING"):
        res = await write_kicker_baselines(db, scored_by_season={2024: _scored([])}, seasons=[2024])
    assert res["rookie_default"] == 1 and res["historical"] == 0
    prof = db.added[0]
    assert prof.profile_source == "kicker_default"
    assert prof.clean_season_baseline["ppr_points"] == pytest.approx(_rookie_default_total())
    assert not any("no K scoring" in r.message for r in caplog.records)   # no vet warn


async def test_write_veteran_no_history_defaults_and_loud_warns(caplog):
    db = _FakeDB([("vk", "Retired Kicker", False)])     # not rookie, no history → unusual
    with caplog.at_level("WARNING"):
        res = await write_kicker_baselines(db, scored_by_season={2024: _scored([])}, seasons=[2024])
    assert res["vet_default"] == 1
    assert db.added[0].clean_season_baseline["ppr_points"] == pytest.approx(_rookie_default_total())
    assert any("no K scoring" in r.message for r in caplog.records)       # loud-warn, not silent


# ---------------------------------------------------------------------------
# the payoff — early-season prior-dominated, late-season invisible
# ---------------------------------------------------------------------------
def test_kicker_prior_dominates_early_season_instead_of_zero():
    """A kicker with a prior but no games yet (Week 1/2) now produces a sane value
    instead of the 0/garbage a null prior gave. Prior ≈ 8.99 ppg (a real kicker)."""
    prior = 152.9 / 17.0   # ≈ 8.99, a historical kicker season-total ÷17
    no_games = pd.DataFrame(columns=["week", "snap_pct", "target_share",
                                     "fantasy_points_ppr", "targets", "carries"])
    v = compute_player_value(
        canonical_player_id="k", name="Some Kicker", position="K",
        weeks=no_games, current_week=2, prior_projection_ppg=prior,
    )
    assert v.forward_ppg == pytest.approx(round(prior, 2))   # prior-only at 0 games
    assert v.forward_value > 0.0                             # NOT 0/garbage


def test_kicker_prior_invisible_late_season():
    """At ≥5 played games the prior weight is 0, so the kicker's value is identical
    with or without the new prior (the week-14 invisibility property)."""
    weeks = pd.DataFrame([
        {"week": w, "snap_pct": 0.0, "target_share": 0.0,
         "fantasy_points_ppr": 8.0, "targets": 0, "carries": 0}
        for w in range(1, 7)                                 # 6 played games
    ])
    common = dict(canonical_player_id="k", name="K", position="K",
                  weeks=weeks, current_week=14)
    with_prior = compute_player_value(**common, prior_projection_ppg=200.0 / 17.0)
    no_prior = compute_player_value(**common, prior_projection_ppg=None)
    assert with_prior.prior_weight == 0.0
    assert with_prior.forward_ppg == pytest.approx(no_prior.forward_ppg)
