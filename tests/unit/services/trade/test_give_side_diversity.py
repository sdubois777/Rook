"""
Give-side DIVERSITY cap (trade_proposals._select_diverse).

A team with one dominant asset (Ben Dover's Josh Allen fv 100) otherwise ships it
in ALL 5 surfaced trades — cheaper-give alternatives clear the gate but rank lower
and get crowded out. The cap limits how many SURFACED trades ship the SAME premium
give-asset, so the set is a mix of strategies. This is PURE surfacing/ranking:
best-first is preserved, only repeats of an already-capped asset are demoted, and
we never pad (fewer diverse trades → surface fewer). The gate/value are untouched.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.trade.trade_proposals import (
    Candidate,
    _MAX_TRADES_PER_GIVE_ASSET,
    _PREMIUM_GIVE_VALUE,
    _select_diverse,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _iv(pid, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=pid, position="WR", forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


# Premium assets (above the bar) + cheaper alternatives + a scrub (below the bar).
_VALUES = {
    "allen": _iv("allen", 100),    # premium
    "kelce": _iv("kelce", 40),     # premium
    "lawrence": _iv("lawrence", 20),  # sub-premium (a cheaper alternative give)
    "dart": _iv("dart", 18),       # sub-premium
    "scrub": _iv("scrub", 8),      # scrub throw-in (well below the bar)
    "getA": _iv("getA", 60), "getB": _iv("getB", 55), "getC": _iv("getC", 50),
}


def _row(give, get, gain):
    """A pre-ranked (Candidate, analysis, edge) row; _select_diverse only reads
    give_ids + values, so analysis/edge are stubbed (gain shown for clarity)."""
    return (Candidate(tuple(give), tuple(get), "opp"), None, gain)


def _gives(selected):
    return [tuple(c.give_ids) for c, _, _ in selected]


def test_premium_bar_sanity():
    assert _VALUES["allen"].forward_value >= _PREMIUM_GIVE_VALUE      # premium
    assert _VALUES["lawrence"].forward_value < _PREMIUM_GIVE_VALUE    # not premium
    assert _VALUES["scrub"].forward_value < _PREMIUM_GIVE_VALUE       # scrub


# ---------------------------------------------------------------------------
# DIVERSITY (headline) — the dominant asset is capped, alternatives fill in
# ---------------------------------------------------------------------------
def test_dominant_asset_is_capped_and_alternatives_surface():
    # best-first: three Allen trades rank highest, then cheaper Lawrence/Dart ones.
    scored = [
        _row(["allen"], ["getA"], 12.0),
        _row(["allen"], ["getB"], 11.0),
        _row(["allen"], ["getC"], 10.0),
        _row(["lawrence"], ["getA"], 6.0),
        _row(["dart"], ["getB"], 5.5),
    ]
    out = _select_diverse(scored, _VALUES, max_results=5)
    allen_trades = [g for g in _gives(out) if "allen" in g]
    assert len(allen_trades) <= _MAX_TRADES_PER_GIVE_ASSET   # ≤2 Allen (was all 5)
    assert any("allen" not in g for g in _gives(out))         # ≥1 non-Allen surfaced
    # best-first preserved among what's taken: the two Allen trades kept are the
    # top two (12, 11), the third Allen (10) skipped, then the alternatives.
    assert _gives(out) == [("allen",), ("allen",), ("lawrence",), ("dart",)]


# ---------------------------------------------------------------------------
# BEST-FIRST preserved — 5 distinct-give trades are unaffected
# ---------------------------------------------------------------------------
def test_five_distinct_give_assets_all_surface_unchanged():
    scored = [
        _row(["allen"], ["getA"], 12.0),
        _row(["kelce"], ["getB"], 11.0),
        _row(["lawrence"], ["getC"], 10.0),
        _row(["dart"], ["getA"], 9.0),
        _row(["scrub"], ["getB"], 8.0),
    ]
    out = _select_diverse(scored, _VALUES, max_results=5)
    assert len(out) == 5                                       # no artificial thinning
    assert _gives(out) == [("allen",), ("kelce",), ("lawrence",), ("dart",), ("scrub",)]


# ---------------------------------------------------------------------------
# NEVER-PAD — only dominant-asset trades clear → surface FEWER, don't pad
# ---------------------------------------------------------------------------
def test_never_pads_when_only_capped_asset_trades_remain():
    scored = [
        _row(["allen"], ["getA"], 12.0),
        _row(["allen"], ["getB"], 11.0),
        _row(["allen"], ["getC"], 10.0),
        _row(["allen", "scrub"], ["getA"], 9.0),
    ]
    out = _select_diverse(scored, _VALUES, max_results=5)
    assert len(out) == _MAX_TRADES_PER_GIVE_ASSET             # 2, NOT padded to 5
    assert all("allen" in g for g in _gives(out))


# ---------------------------------------------------------------------------
# PREMIUM BAR — a scrub throw-in doesn't trigger the cap; Allen does
# ---------------------------------------------------------------------------
def test_scrub_throwin_counts_against_the_premium_asset_not_the_scrub():
    # Each trade ships Allen + a scrub. The cap must count Allen (premium), not the
    # scrub — so only 2 surface (Allen capped), NOT unlimited-because-of-the-scrub.
    scored = [
        _row(["allen", "scrub"], ["getA"], 12.0),
        _row(["allen", "scrub"], ["getB"], 11.0),
        _row(["allen", "scrub"], ["getC"], 10.0),
    ]
    out = _select_diverse(scored, _VALUES, max_results=5)
    assert len(out) == _MAX_TRADES_PER_GIVE_ASSET             # Allen capped at 2


def test_two_sub_premium_gives_never_trigger_the_cap():
    # Trades of only low-value players (all below the premium bar) are never capped
    # — surface as many as clear (best-first), no diversity throttle.
    scored = [
        _row(["lawrence", "scrub"], ["getA"], 7.0),
        _row(["lawrence", "scrub"], ["getB"], 6.5),
        _row(["dart", "scrub"], ["getC"], 6.0),
        _row(["lawrence"], ["getA"], 5.5),
    ]
    out = _select_diverse(scored, _VALUES, max_results=5)
    assert len(out) == 4                                       # none capped (all sub-premium)


def test_multi_premium_give_counts_against_each_asset():
    # A trade shipping TWO premium assets (allen + kelce) counts against BOTH.
    scored = [
        _row(["allen", "kelce"], ["getA"], 12.0),   # uses allen#1 + kelce#1
        _row(["allen"], ["getB"], 11.0),            # allen#2
        _row(["kelce"], ["getC"], 10.0),            # kelce#2
        _row(["allen"], ["getA"], 9.0),             # allen#3 → SKIP (capped)
        _row(["kelce"], ["getB"], 8.5),             # kelce#3 → SKIP (capped)
        _row(["lawrence"], ["getC"], 8.0),          # sub-premium → taken
    ]
    out = _select_diverse(scored, _VALUES, max_results=5)
    assert _gives(out) == [("allen", "kelce"), ("allen",), ("kelce",), ("lawrence",)]


# ---------------------------------------------------------------------------
# END-TO-END on the real seed (guarded) — Ben Dover no longer spams Allen
# ---------------------------------------------------------------------------
_WEEKLY_CACHE = Path("data/cache/weekly_pbp_2025.parquet")


@pytest.mark.skipif(
    not _WEEKLY_CACHE.exists(),
    reason="real 2025 per-week data not on disk (CI) — helper tests cover the logic",
)
async def test_ben_dover_no_longer_ships_allen_in_every_trade():
    from backend.database import AsyncSessionLocal
    from backend.services.trade.trade_demo_source import seed_demo_league
    from backend.services.trade.value_engine import evaluate_league
    from backend.services.trade.trade_proposals import enumerate_candidates, evaluate_candidates

    try:
        async with AsyncSessionLocal() as db:
            src = await seed_demo_league(db)
    except Exception as exc:
        pytest.skip(f"demo DB unavailable: {exc}")

    state = src.get_league_state()
    vals = evaluate_league(state, src.weekly_usage, priors=src.priors)
    bd = next(t for t in state.teams if t.team_name == "Ben Dover")
    allen = next(rp.canonical_player_id for rp in bd.roster if rp.name == "Josh Allen")

    surfaced = evaluate_candidates(
        state, vals, bd.team_id, enumerate_candidates(state, vals, bd.team_id), roster_limit=16)
    allen_trades = [c for c, _, _ in surfaced if allen in c.give_ids]
    assert len(allen_trades) <= _MAX_TRADES_PER_GIVE_ASSET   # ≤2 (was all 5)
    assert any(allen not in c.give_ids for c, _, _ in surfaced)   # ≥1 diverse alternative
    # every surfaced trade still cleared the full gate (selection only, gate intact)
    assert all(e.clears for _, _, e in surfaced)
