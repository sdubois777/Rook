"""
Explain-the-silence tests (trade_proposals.build_silence_context).

When a team surfaces 0 trades, classify WHY from the candidates' cond1-4 pattern
(reusing the SAME edge-band gate) and, if one is close enough, attach the nearest
near-miss as a NEGOTIATION STARTER (not a recommendation). Presentation only — this
never changes what surfaces.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_proposals import (
    Candidate,
    _NEAR_MISS_PPG,
    build_silence_context,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _iv(pid, pos, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=pid, position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _team(tid, name, is_me, spec):
    return TeamState(tid, name, is_me,
                     tuple(RosterPlayer(p, p, pos) for p, pos, _ in spec))


def _state(*team_specs):
    teams = tuple(_team(tid, name, is_me, spec) for tid, name, is_me, spec in team_specs)
    values = {p: _iv(p, pos, fv)
              for _, _, _, spec in team_specs for p, pos, fv in spec}
    return LeagueState(2025, 14, teams), values


# A full 1QB/2RB/3WR/1TE/1FLEX roster template (fv doubles as ppg here).
def _roster(prefix, qb, rb1, rb2, rb3, wr1, wr2, wr3, te, flexbench):
    return [
        (f"{prefix}qb", "QB", qb), (f"{prefix}rb1", "RB", rb1), (f"{prefix}rb2", "RB", rb2),
        (f"{prefix}rb3", "RB", rb3), (f"{prefix}wr1", "WR", wr1), (f"{prefix}wr2", "WR", wr2),
        (f"{prefix}wr3", "WR", wr3), (f"{prefix}te", "TE", te), (f"{prefix}bn", "WR", flexbench),
    ]


# ---------------------------------------------------------------------------
# REASON CLASSIFICATION
# ---------------------------------------------------------------------------
def test_lineup_too_strong_when_best_gain_below_threshold_and_team_is_strong():
    # A stacked team (all elite) + a weak opponent: no acquisition improves the
    # strong team's lineup by ≥5. Dominant failure = cant_improve, and the team is
    # above the league median → lineup_too_strong.
    strong = _roster("s", 40, 90, 88, 40, 92, 88, 84, 60, 30)
    weak = _roster("w", 8, 12, 10, 5, 11, 9, 7, 6, 4)
    state, values = _state(("me", "Strong", True, strong), ("opp", "Weak", False, weak))
    # candidates: give my surplus/bench for their (worse) players — none improve me.
    cands = [Candidate(("sbn",), ("wwr1",), "opp"), Candidate(("srb3",), ("wrb1",), "opp")]
    sc = build_silence_context(state, values, "me", cands, roster_limit=16)
    assert sc.reason == "lineup_too_strong"
    assert "strong enough" in sc.message


def test_asset_poor_when_weak_team_cant_improve():
    # A weak team (below league median) whose only trades are LATERAL — acquiring
    # the opponent's scrubs that don't beat its own weak starters → cond1 fails
    # (cant_improve dominant) and the team is below median → asset_poor.
    me = [("mqb", "QB", 8), ("mrb1", "RB", 12), ("mrb2", "RB", 10), ("mrb3", "RB", 5),
          ("mwr1", "WR", 11), ("mwr2", "WR", 9), ("mwr3", "WR", 7), ("mte", "TE", 6),
          ("mbn", "WR", 4)]
    opp = [("oqb", "QB", 40), ("orb1", "RB", 90), ("orb2", "RB", 88), ("owr1", "WR", 92),
           ("owr2", "WR", 88), ("owr3", "WR", 84), ("ote", "TE", 60),
           ("oscrubwr", "WR", 5), ("oscrubrb", "RB", 6)]   # opp is strong → I'm below median
    state, values = _state(("me", "Weak", True, me), ("opp", "Strong", False, opp))
    # acquire the opp's scrubs (5/6) — they don't beat my starters (7/5) → no gain.
    cands = [Candidate(("mbn",), ("oscrubwr",), "opp"), Candidate(("mrb3",), ("oscrubrb",), "opp")]
    sc = build_silence_context(state, values, "me", cands, roster_limit=16)
    assert sc.reason == "asset_poor"


def test_scarcity_when_cond1_passers_are_blocked_by_cond2():
    # A middling team whose candidates DO improve its lineup (cond1 pass) but crater
    # the opponent (cond2 fail) — the upgrade studs are locked into starting
    # lineups → scarcity. Build it so the acquired players are the opponent's
    # STARTERS (giving them up drops the opponent's lineup) and I'm WR-thin so they
    # improve me a lot.
    me = [("mqb", "QB", 30), ("mrb1", "RB", 30), ("mrb2", "RB", 28),
          ("mwr1", "WR", 30), ("mwr2", "WR", 6), ("mwr3", "WR", 5),
          ("mte", "TE", 28), ("mbn1", "RB", 26), ("mbn2", "RB", 25)]
    opp = [("oqb", "QB", 28), ("orb1", "RB", 9), ("orb2", "RB", 8),
           ("owr1", "WR", 40), ("owr2", "WR", 38), ("owr3", "WR", 36), ("owr4", "WR", 34),
           ("ote", "TE", 9)]
    state, values = _state(("me", "Mid", True, me), ("opp", "WRloaded", False, opp))
    # give my surplus RB (helps their RB need) for their WR STARTER (fills my WR hole)
    cands = [Candidate(("mbn1",), ("owr1",), "opp"), Candidate(("mbn2",), ("owr2",), "opp")]
    sc = build_silence_context(state, values, "me", cands, roster_limit=16)
    assert sc.reason == "scarcity"
    assert "locked into" in sc.message


# ---------------------------------------------------------------------------
# NEAR-MISS THRESHOLD + FRAMING
# ---------------------------------------------------------------------------
def test_near_miss_present_when_within_threshold():
    # A trade that improves the team ~4 ppg (short of 5 by <10) → near-miss present.
    me = _roster("m", 30, 40, 38, 20, 36, 34, 8, 30, 12)   # WR3 weak (8) → upgradeable
    opp = _roster("o", 28, 9, 8, 6, 20, 18, 16, 9, 7)      # surplus WRs to acquire
    state, values = _state(("me", "Me", True, me), ("opp", "Opp", False, opp))
    cands = [Candidate(("mbn",), ("owr3",), "opp"), Candidate(("mrb3",), ("owr2",), "opp")]
    sc = build_silence_context(state, values, "me", cands, roster_limit=16)
    assert sc.near_miss is not None
    assert 0 <= sc.near_miss.would_be_ppg               # carries the would-be gain
    assert sc.near_miss.shortfall_reason                # carries a reason
    # margin actually within the constant
    assert _NEAR_MISS_PPG == 10.0


def test_no_near_miss_when_closest_miss_exceeds_threshold():
    # Every candidate is a wild fleece/mismatch that misses by a mile → no near-miss.
    me = _roster("m", 30, 40, 38, 20, 36, 34, 30, 30, 12)   # already strong at WR
    opp = _roster("o", 28, 9, 8, 6, 20, 18, 16, 9, 7)
    state, values = _state(("me", "Me", True, me), ("opp", "Opp", False, opp))
    # give my studs for their scrubs: lineup would DROP hugely (huge negative gain)
    cands = [Candidate(("mrb1",), ("orb3",), "opp"), Candidate(("mwr1",), ("owr3",), "opp")]
    sc = build_silence_context(state, values, "me", cands, roster_limit=16)
    assert sc.near_miss is None                         # nothing close enough
    assert sc.reason in {"lineup_too_strong", "asset_poor", "no_fair_trade", "scarcity"}


def test_no_candidates_falls_back_to_team_strength():
    strong = _roster("s", 40, 90, 88, 40, 92, 88, 84, 60, 30)
    weak = _roster("w", 8, 12, 10, 5, 11, 9, 7, 6, 4)
    state, values = _state(("me", "Strong", True, strong), ("opp", "Weak", False, weak))
    sc = build_silence_context(state, values, "me", [], roster_limit=16)
    assert sc.reason == "lineup_too_strong" and sc.near_miss is None


# ---------------------------------------------------------------------------
# END-TO-END on the real seed (guarded) — Watson's is lineup_too_strong
# ---------------------------------------------------------------------------
_WEEKLY_CACHE = Path("data/cache/weekly_pbp_2025.parquet")


@pytest.mark.skipif(
    not _WEEKLY_CACHE.exists(),
    reason="real 2025 per-week data not on disk (CI) — synthetic tests cover the logic",
)
async def test_watsons_silence_is_lineup_too_strong_on_real_seed():
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
    w = next(t for t in state.teams if t.team_name == "Watson's Rub and...")
    cands = enumerate_candidates(state, vals, w.team_id)
    assert evaluate_candidates(state, vals, w.team_id, cands, roster_limit=16) == []  # silent
    sc = build_silence_context(state, vals, w.team_id, cands, roster_limit=16)
    assert sc.reason == "lineup_too_strong"
