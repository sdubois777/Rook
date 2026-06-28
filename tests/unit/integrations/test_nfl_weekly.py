"""
tests/unit/integrations/test_nfl_weekly.py

Unit tests for the per-week NFL data layer (backend/integrations/nfl_weekly.py).

Two tiers:
  * Synthetic tests — build a tiny PBP/snaps frame with hand-known plays and
    assert the per-week shape, the fantasy-point formula, the per-week target
    share, and the canonical-id join. No network, no DB — these run in CI.
  * Real-2025 spot-check (guarded) — if the gitignored 2025 caches are present
    (i.e. the layer has been built locally), prove that the per-week table,
    summed over weeks, EXACTLY reproduces the verified season function, and
    that the canonical join resolves a real player end-to-end. Skips cleanly in
    CI where the cache is absent.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.integrations.nfl_weekly import (
    _norm_id,
    attach_canonical_ids,
    build_weekly_usage,
    compute_weekly_pbp,
    compute_weekly_snaps,
)

CACHE_DIR = Path("data/cache")


# ---------------------------------------------------------------------------
# Synthetic PBP builder
# ---------------------------------------------------------------------------
def _play(week, posteam, **kw):
    base = dict(
        season_type="REG", week=week, posteam=posteam, game_id=f"{week}_{posteam}",
        receiver_player_id=None, receiver_player_name=None,
        rusher_player_id=None, rusher_player_name=None,
        passer_player_id=None, passer_player_name=None,
        pass_attempt=0, complete_pass=0, touchdown=0, pass_touchdown=0,
        interception=0, fumble_lost=0,
        fumbled_1_player_id=None, fumbled_1_player_name=None,
        receiving_yards=0.0, rushing_yards=0.0, passing_yards=0.0,
    )
    base.update(kw)
    return base


@pytest.fixture
def synthetic_pbp() -> pd.DataFrame:
    """Team AAA, weeks 1-2. WR1 (3 tgt/2 rec/50 yd/1 TD wk1; 1/1/40 wk2),
    WR2 (1 tgt wk1), RB1 (2 car/20 yd/1 TD + nothing receiving wk1),
    QB1 (250 pass yd / 2 TD / 1 INT wk1) and a lost fumble."""
    rows = [
        _play(1, "AAA", receiver_player_id="W1", receiver_player_name="WR One",
              pass_attempt=1, complete_pass=1, receiving_yards=20.0),
        _play(1, "AAA", receiver_player_id="W1", receiver_player_name="WR One",
              pass_attempt=1, complete_pass=1, receiving_yards=30.0, touchdown=1),
        _play(1, "AAA", receiver_player_id="W1", receiver_player_name="WR One",
              pass_attempt=1),  # incomplete target
        _play(1, "AAA", receiver_player_id="W2", receiver_player_name="WR Two",
              pass_attempt=1, complete_pass=1, receiving_yards=10.0),
        _play(1, "AAA", rusher_player_id="R1", rusher_player_name="RB One",
              rushing_yards=5.0),
        _play(1, "AAA", rusher_player_id="R1", rusher_player_name="RB One",
              rushing_yards=15.0, touchdown=1),
        _play(1, "AAA", passer_player_id="Q1", passer_player_name="QB One",
              passing_yards=250.0, pass_touchdown=1),
        _play(1, "AAA", passer_player_id="Q1", passer_player_name="QB One",
              pass_touchdown=1),
        _play(1, "AAA", passer_player_id="Q1", passer_player_name="QB One",
              interception=1),
        _play(1, "AAA", fumble_lost=1,
              fumbled_1_player_id="R1", fumbled_1_player_name="RB One"),
        _play(2, "AAA", receiver_player_id="W1", receiver_player_name="WR One",
              pass_attempt=1, complete_pass=1, receiving_yards=40.0),
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Per-week shape + scoring
# ---------------------------------------------------------------------------
def test_weekly_pbp_is_per_week_not_collapsed(synthetic_pbp):
    """The whole point of the slice: multiple rows per player across weeks."""
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    w1 = wk[wk.player_id == "W1"]
    assert sorted(w1.week.tolist()) == [1, 2]  # not one season row
    assert (wk["season"] == 2025).all()
    assert set(wk["week"]) == {1, 2}


def test_weekly_pbp_receiving_fantasy_points_ppr_and_std(synthetic_pbp):
    """WR1 wk1: 2 rec + 50 yds + 1 TD = 2 + 5 + 6 = 13.0 PPR; std drops the 2
    reception points = 11.0."""
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    row = wk[(wk.player_id == "W1") & (wk.week == 1)].iloc[0]
    assert row.fantasy_points_ppr == pytest.approx(13.0)
    assert row.fantasy_points_std == pytest.approx(11.0)


def test_weekly_pbp_rushing_points_and_carries(synthetic_pbp):
    """RB1 wk1: 20 rush yds + 1 rush TD = 2 + 6 = 8.0, minus a lost fumble
    (-2) = 6.0; carries == 2 (the fumble is a separate play, not a rush)."""
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    row = wk[(wk.player_id == "R1") & (wk.week == 1)].iloc[0]
    assert row.carries == 2
    assert row.fumbles_lost == 1
    assert row.fantasy_points_ppr == pytest.approx(6.0)  # 8 rushing − 2 fumble


def test_weekly_pbp_passing_td_and_interception(synthetic_pbp):
    """QB1 wk1: 250 pass yds*.04 + 2 pass TD*4 − 1 INT*2 = 10 + 8 − 2 = 16.0."""
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    row = wk[(wk.player_id == "Q1") & (wk.week == 1)].iloc[0]
    assert row.passing_tds == 2
    assert row.interceptions == 1
    assert row.fantasy_points_ppr == pytest.approx(16.0)


def test_weekly_pbp_target_share_uses_weekly_team_denominator(synthetic_pbp):
    """Per-week share = player targets / team targets that week. Wk1 AAA has 4
    targets (3 to W1, 1 to W2) → W1 0.75, W2 0.25. Wk2 only W1 targeted → 1.0."""
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    assert wk[(wk.player_id == "W1") & (wk.week == 1)].iloc[0].target_share == pytest.approx(0.75)
    assert wk[(wk.player_id == "W2") & (wk.week == 1)].iloc[0].target_share == pytest.approx(0.25)
    assert wk[(wk.player_id == "W1") & (wk.week == 2)].iloc[0].target_share == pytest.approx(1.0)


def test_weekly_pbp_week_share_averages_to_season_share(synthetic_pbp):
    """Apples-to-apples guarantee: the season target share is the mean of the
    per-week shares (W1: mean(0.75, 1.0) = 0.875)."""
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    w1 = wk[wk.player_id == "W1"]
    assert w1.target_share.mean() == pytest.approx((0.75 + 1.0) / 2)


# ---------------------------------------------------------------------------
# Per-week snaps
# ---------------------------------------------------------------------------
def test_weekly_snaps_per_week_skill_only_reg_only():
    snaps = pd.DataFrame([
        dict(pfr_player_id="P1", player="WR One", position="WR", team="AAA",
             season=2025, week=1, game_type="REG", offense_snaps=50, offense_pct=0.9),
        dict(pfr_player_id="P1", player="WR One", position="WR", team="AAA",
             season=2025, week=2, game_type="REG", offense_snaps=40, offense_pct=0.8),
        dict(pfr_player_id="L1", player="Left Tackle", position="T", team="AAA",
             season=2025, week=1, game_type="REG", offense_snaps=60, offense_pct=1.0),
        dict(pfr_player_id="P1", player="WR One", position="WR", team="AAA",
             season=2025, week=19, game_type="POST", offense_snaps=55, offense_pct=0.95),
    ])
    out = compute_weekly_snaps(2025, snaps=snaps, use_cache=False)
    assert sorted(out[out.pfr_player_id == "P1"].week.tolist()) == [1, 2]  # POST dropped
    assert "T" not in out.position.tolist()  # non-skill dropped
    assert out[out.week == 1].iloc[0].snap_pct == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Canonical id join
# ---------------------------------------------------------------------------
def _bridge():
    return pd.DataFrame([
        {"gsis_id": "W1", "pfr_id": "pW1", "sleeper_id": "111", "sportradar_id": "srA"},
        {"gsis_id": "R1", "pfr_id": "pR1", "sleeper_id": None, "sportradar_id": "srB"},
        {"gsis_id": "X9", "pfr_id": "pX9", "sleeper_id": "999", "sportradar_id": "srZ"},
    ])


def test_norm_id_strips_float_artifact():
    assert _norm_id(7564.0) == "7564"
    assert _norm_id("7564") == "7564"
    assert _norm_id(None) is None
    assert _norm_id(float("nan")) is None


def test_attach_canonical_ids_priority_sleeper_then_sportradar():
    """W1 resolves via sleeper (preferred); R1 has no sleeper id so it falls
    back to sportradar."""
    df = pd.DataFrame({"player_id": ["W1", "R1"], "week": [1, 1]})
    maps = {"sleeper": {"111": "uuid-w1"}, "sportradar": {"srB": "uuid-r1"}, "gsis": {}}
    out = attach_canonical_ids(df, "player_id", "gsis", bridge=_bridge(), player_maps=maps)
    assert out.set_index("player_id").canonical_player_id.to_dict() == {
        "W1": "uuid-w1", "R1": "uuid-r1",
    }


def test_attach_canonical_ids_unmatched_is_none():
    """A player not in any Rook id map resolves to None (kept, not crashed)."""
    df = pd.DataFrame({"player_id": ["X9"], "week": [1]})
    maps = {"sleeper": {}, "sportradar": {}, "gsis": {}}
    out = attach_canonical_ids(df, "player_id", "gsis", bridge=_bridge(), player_maps=maps)
    assert out.iloc[0].canonical_player_id is None


def test_attach_canonical_ids_pfr_for_snaps():
    """Snaps key on pfr_player_id; the bridge maps pfr → sleeper → Rook uuid."""
    df = pd.DataFrame({"pfr_player_id": ["pW1"], "week": [1]})
    maps = {"sleeper": {"111": "uuid-w1"}, "sportradar": {}, "gsis": {}}
    out = attach_canonical_ids(df, "pfr_player_id", "pfr", bridge=_bridge(), player_maps=maps)
    assert out.iloc[0].canonical_player_id == "uuid-w1"


def test_build_weekly_usage_merges_pbp_and_snaps(synthetic_pbp):
    """The combined table is keyed (canonical_player_id, week) and carries both
    snap_pct (from snaps) and target_share/fantasy (from PBP)."""
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    snaps = pd.DataFrame([
        dict(pfr_player_id="pW1", player="WR One", position="WR", team="AAA",
             season=2025, week=1, game_type="REG", offense_snaps=50, offense_pct=0.88),
    ])
    snap_wk = compute_weekly_snaps(2025, snaps=snaps, use_cache=False)
    bridge = pd.DataFrame([
        {"gsis_id": "W1", "pfr_id": "pW1", "sleeper_id": "111", "sportradar_id": "srA"},
    ])
    maps = {"sleeper": {"111": "uuid-w1"}, "sportradar": {}, "gsis": {}}
    usage = build_weekly_usage(
        2025, maps, bridge=bridge, pbp_weekly=wk, snaps_weekly=snap_wk,
    )
    w1 = usage[usage.canonical_player_id == "uuid-w1"]
    assert sorted(w1.week.tolist()) == [1, 2]  # per-week preserved
    row1 = w1[w1.week == 1].iloc[0]
    assert row1.snap_pct == pytest.approx(0.88)        # from snaps
    assert row1.target_share == pytest.approx(0.75)    # from PBP
    assert row1.fantasy_points_ppr == pytest.approx(13.0)


def test_build_weekly_usage_week_filter(synthetic_pbp):
    wk = compute_weekly_pbp(2025, pbp=synthetic_pbp, use_cache=False)
    bridge = pd.DataFrame([
        {"gsis_id": "W1", "pfr_id": "pW1", "sleeper_id": "111", "sportradar_id": "srA"},
    ])
    maps = {"sleeper": {"111": "uuid-w1"}, "sportradar": {}, "gsis": {}}
    usage = build_weekly_usage(
        2025, maps, bridge=bridge, pbp_weekly=wk, snaps_weekly=pd.DataFrame(), weeks=[2],
    )
    assert set(usage.week) == {2}


# ---------------------------------------------------------------------------
# Real-2025 spot-check (guarded — skips when the gitignored cache is absent)
# ---------------------------------------------------------------------------
_WEEKLY_CACHE = CACHE_DIR / "weekly_pbp_2025.parquet"
_SEASON_CACHE = CACHE_DIR / "seasonal_pbp_2025.pkl"
_BRIDGE_CACHE = CACHE_DIR / "nflverse_id_bridge.parquet"


@pytest.mark.skipif(
    not (_WEEKLY_CACHE.exists() and _SEASON_CACHE.exists()),
    reason="2025 PBP caches not present (build the layer locally to run this)",
)
def test_real_2025_weekly_sum_equals_verified_season():
    """The per-week table, summed over weeks, must reproduce the verified season
    function exactly — that is the apples-to-apples guarantee for trajectory."""
    import pickle

    wk = pd.read_parquet(_WEEKLY_CACHE)
    with open(_SEASON_CACHE, "rb") as fh:
        season = pickle.load(fh)

    # Shape: genuinely per-week, not collapsed.
    assert wk.groupby("player_id")["week"].nunique().max() > 1
    assert wk["week"].nunique() >= 17

    season_pts = season.set_index("player_id")["fantasy_points_ppr"].to_dict()
    wk_sum = wk.groupby("player_id")["fantasy_points_ppr"].sum().round(2)
    for name in ("McCaffrey", "Chase", "Nacua"):
        cand = season[season["player_display_name"].str.contains(name, na=False)]
        cand = cand.sort_values("fantasy_points_ppr", ascending=False)
        assert len(cand), f"{name} not found in season data"
        pid = cand.iloc[0]["player_id"]
        assert wk_sum[pid] == pytest.approx(season_pts[pid], abs=0.1)


@pytest.mark.skipif(
    not (_WEEKLY_CACHE.exists() and _BRIDGE_CACHE.exists()),
    reason="2025 weekly / id-bridge caches not present",
)
def test_real_2025_canonical_join_resolves_known_player():
    """End-to-end: Ja'Marr Chase's real gsis id (00-0036900) joins through the
    cached crosswalk to a Rook uuid — a real player is not dropped on the floor."""
    wk = pd.read_parquet(_WEEKLY_CACHE)
    bridge = pd.read_parquet(_BRIDGE_CACHE)
    # Chase's real Rook ids (sleeper 7564 / gsis 00-0036900) → a stand-in uuid.
    maps = {"sleeper": {"7564": "uuid-chase"}, "sportradar": {}, "gsis": {}}
    out = attach_canonical_ids(wk, "player_id", "gsis", bridge=bridge, player_maps=maps)
    chase = out[out.player_id == "00-0036900"]
    assert len(chase) >= 1
    assert (chase["canonical_player_id"] == "uuid-chase").all()
