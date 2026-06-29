"""
Tests for the FLAG-GATED demo trade harness (trade_demo_source.py).

Three tiers:
  * GATING (CI) — TRADE_DEMO_MODE off ⇒ the demo provider is never selected.
  * ASSEMBLY + tier coverage (CI) — the demo rosters + injected synthetic weekly
    data (mirroring the real-cast shapes) run through the SAME builders + engine;
    asserts every confidence tier + buy/sell + team-change flag appears.
  * REAL seed (guarded) — seeds from the real DB + #149 layer and asserts the
    actual planted players' tiers; skips where the data/DB is absent (CI).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.services.trade.trade_demo_source import (
    DEMO_CURRENT_WEEK,
    DEMO_ROSTERS,
    DEMO_SEASON,
    build_league_state,
    build_priors,
    maybe_demo_league_source,
    seed_demo_league,
    trade_demo_enabled,
    TradeDemoSource,
)
from backend.services.trade.value_engine import (
    Confidence,
    ValueTrend,
    evaluate_league,
)


# ---------------------------------------------------------------------------
# GATING (pure, CI)
# ---------------------------------------------------------------------------
def test_trade_demo_enabled_reflects_env(monkeypatch):
    monkeypatch.setenv("TRADE_DEMO_MODE", "true")
    assert trade_demo_enabled() is True
    monkeypatch.setenv("TRADE_DEMO_MODE", "false")
    assert trade_demo_enabled() is False
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    assert trade_demo_enabled() is False


async def test_gate_off_never_selects_demo_provider(monkeypatch):
    """With the flag off the demo source is not built and the DB is never touched
    (a sentinel db would raise if used)."""
    monkeypatch.delenv("TRADE_DEMO_MODE", raising=False)
    result = await maybe_demo_league_source(db=object())
    assert result is None


# ---------------------------------------------------------------------------
# ASSEMBLY + tier coverage (CI, synthetic data through the real code paths)
# ---------------------------------------------------------------------------
def _series(tier: str):
    """Return (rows, prior_ppg) for a tier, mirroring the real-cast shapes."""
    def rows(snaps, targets, points, *, teams=None, weeks=None, tgts=None, carries=None):
        n = len(snaps)
        weeks = weeks or list(range(1, n + 1))
        teams = teams or ["AAA"] * n
        return [
            {"week": weeks[i], "snap_pct": snaps[i], "target_share": targets[i],
             "fantasy_points_ppr": points[i], "targets": (tgts or [0] * n)[i],
             "carries": (carries or [0] * n)[i], "nfl_team": teams[i]}
            for i in range(n)
        ]

    if tier == "stud":
        return rows([0.82] * 12, [0.22] * 12, [18] * 12), 19.0
    if tier == "buy":   # rising usage → buy_low, full
        return rows([0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.78, 0.85, 0.88, 0.90, 0.92],
                    [0.08, 0.09, 0.10, 0.12, 0.14, 0.16, 0.18, 0.22, 0.25, 0.27, 0.28, 0.30],
                    [4, 5, 5, 6, 7, 8, 9, 12, 14, 15, 16, 17],
                    tgts=[3, 3, 4, 5, 6, 7, 8, 10, 12, 12, 13, 14]), 14.0
    if tier == "sell":  # falling usage → sell_high, full (null prior, like real Renfrow)
        return rows([0.85, 0.80, 0.78, 0.55, 0.45, 0.40],
                    [0.26, 0.24, 0.22, 0.12, 0.10, 0.09],
                    [16, 15, 14, 8, 7, 6], tgts=[9, 8, 8, 4, 3, 3]), None
    if tier == "partial":   # 3 played weeks → limited
        return rows([0.5, 0.5, 0.5], [0.10, 0.10, 0.10], [8, 7, 9], tgts=[5, 5, 5]), None
    if tier == "sparse":    # 1 played week → insufficient
        return rows([0.4], [0.08], [5], tgts=[3]), None
    if tier == "teamchange":  # AAA→BBB within window → limited + flag
        return rows([0.50, 0.50, 0.50, 0.85, 0.90],
                    [0.10, 0.10, 0.10, 0.26, 0.28], [6, 6, 6, 16, 18],
                    teams=["AAA", "AAA", "AAA", "BBB", "BBB"], tgts=[4, 4, 4, 10, 11]), None
    if tier == "rookie":    # real rookie, NO prior → prior_weight 0
        return rows([0.30] * 9, [0.08] * 9, [4] * 9, tgts=[3] * 9), None
    raise ValueError(tier)


# Which planted name represents which tier (mirrors the real casting).
_NAME_TIER = {
    "Christian McCaffrey": "stud", "Puka Nacua": "stud", "A.J. Brown": "buy",
    "Najee Harris": "partial", "DJ Turner": "sparse",
    "Jahmyr Gibbs": "stud", "Jonathan Taylor": "stud", "Hunter Renfrow": "sell",
    "Brandin Cooks": "teamchange", "Darius Cooper": "rookie",
    "Ja'Marr Chase": "stud", "Bijan Robinson": "stud", "Trey McBride": "stud",
    "George Pickens": "stud", "Jayden Reed": "partial",
}


@pytest.fixture
def synthetic_demo_source() -> TradeDemoSource:
    """Assemble a TradeDemoSource over the REAL DEMO_ROSTERS using synthetic ids
    + injected weekly data — exercises build_league_state / build_priors and the
    engine without a DB."""
    name_to_player: dict[str, tuple[str, float | None]] = {}
    frames = []
    for i, (name, tier) in enumerate(_NAME_TIER.items()):
        pid = f"u-{i:02d}"
        rows, prior = _series(tier)
        name_to_player[name] = (pid, prior)
        df = pd.DataFrame(rows)
        df["canonical_player_id"] = pid
        df["player_name"] = name
        df["position"] = "WR"
        frames.append(df)
    weekly = pd.concat(frames, ignore_index=True)
    state = build_league_state(name_to_player)
    priors = build_priors(name_to_player)
    return TradeDemoSource(state=state, weekly_usage=weekly, priors=priors)


def _values_by_name(source: TradeDemoSource):
    values = evaluate_league(source.get_league_state(), source.weekly_usage, priors=source.priors)
    by_name = {}
    for team in source.get_league_state().teams:
        for rp in team.roster:
            if rp.canonical_player_id in values:
                by_name[rp.name] = values[rp.canonical_player_id]
    return values, by_name


def test_demo_state_has_an_is_me_team_and_full_rosters(synthetic_demo_source):
    state = synthetic_demo_source.get_league_state()
    assert state.season == DEMO_SEASON and state.week == DEMO_CURRENT_WEEK
    assert state.my_team is not None and state.my_team.team_name == "You"
    assert len(state.teams) == len(DEMO_ROSTERS)
    assert sum(len(t.roster) for t in state.teams) == sum(len(s["players"]) for s in DEMO_ROSTERS)


def test_every_confidence_tier_is_exercised(synthetic_demo_source):
    values, _ = _values_by_name(synthetic_demo_source)
    confs = {v.confidence for v in values.values()}
    assert Confidence.FULL in confs
    assert Confidence.LIMITED in confs
    assert Confidence.INSUFFICIENT in confs


def test_studs_full_sparse_insufficient_partial_limited(synthetic_demo_source):
    _, by_name = _values_by_name(synthetic_demo_source)
    assert by_name["Christian McCaffrey"].confidence is Confidence.FULL
    assert by_name["Puka Nacua"].confidence is Confidence.FULL
    # sparse player: insufficient AND not given a confident buy/sell
    dj = by_name["DJ Turner"]
    assert dj.confidence is Confidence.INSUFFICIENT
    assert dj.buy_low is False and dj.sell_high is False
    # partial player: limited
    assert by_name["Najee Harris"].confidence is Confidence.LIMITED


def test_team_change_player_limited_with_direction_suppressed(synthetic_demo_source):
    """Team change → confidence limited AND the actionable buy/sell flags are
    suppressed (cross-team share delta isn't a real trajectory)."""
    _, by_name = _values_by_name(synthetic_demo_source)
    cooks = by_name["Brandin Cooks"]
    assert cooks.confidence is Confidence.LIMITED
    assert "team change" in cooks.confidence_reason
    assert cooks.buy_low is False and cooks.sell_high is False


def test_buy_low_and_sell_high_fire_with_sane_why(synthetic_demo_source):
    _, by_name = _values_by_name(synthetic_demo_source)
    ajb = by_name["A.J. Brown"]
    assert ajb.buy_low is True and ajb.value_trend is ValueTrend.RISING
    assert ajb.why and "rising" in ajb.why
    renfrow = by_name["Hunter Renfrow"]
    assert renfrow.sell_high is True and renfrow.value_trend is ValueTrend.FALLING
    assert renfrow.why


def test_null_prior_player_has_zero_prior_weight(synthetic_demo_source):
    """The real-rookie / unprofiled players carry no prior → prior_weight 0."""
    _, by_name = _values_by_name(synthetic_demo_source)
    assert by_name["Darius Cooper"].prior_weight == 0.0
    assert "Darius Cooper" not in synthetic_demo_source.priors


# ---------------------------------------------------------------------------
# REAL seed (guarded — runs locally with the 2025 data, skips in CI)
# ---------------------------------------------------------------------------
_WEEKLY_CACHE = Path("data/cache/weekly_pbp_2025.parquet")


@pytest.mark.skipif(
    not _WEEKLY_CACHE.exists(),
    reason="real 2025 per-week data not on disk (CI) — synthetic test covers logic",
)
async def test_real_demo_seed_produces_sane_tiers():
    from backend.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            source = await seed_demo_league(db)
    except Exception as exc:  # no DB / not populated → skip, don't fail
        pytest.skip(f"demo DB unavailable: {exc}")

    values, by_name = _values_by_name(source)
    confs = {v.confidence for v in values.values()}

    assert {Confidence.FULL, Confidence.LIMITED, Confidence.INSUFFICIENT} <= confs
    assert by_name["Christian McCaffrey"].confidence is Confidence.FULL
    assert by_name["DJ Turner"].confidence is Confidence.INSUFFICIENT
    assert by_name["DJ Turner"].buy_low is False and by_name["DJ Turner"].sell_high is False
    cooks = by_name["Brandin Cooks"]
    assert "team change" in cooks.confidence_reason
    assert cooks.buy_low is False and cooks.sell_high is False  # direction suppressed
    assert by_name["A.J. Brown"].buy_low is True
    assert by_name["Hunter Renfrow"].sell_high is True
