"""
Player-availability / staleness tests (docs/trade_value_availability_design.md).

The bug: the value window operates over games PLAYED with no notion of WHEN, so an
injured player keeps pre-injury value forever (Kraft: out since wk9, read 70.9
"sell-high" at wk14). The fix keys on weeks_stale (weeks since last game, byes
EXCLUDED) and applies TWO separate mechanics — staleness → confidence (dampening
the #170 trend factor for free) and staleness → base-level decay (the actual
inflation fix). These are the §7 paired safety set: the injury cases must crater
AND the bye / healthy-stud cases must stay UNCHANGED, together.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from backend.services.trade.value_engine import (
    Confidence,
    ValueTrend,
    _assess_confidence,
    _staleness_decay,
    bye_weeks_from_schedule,
    compute_player_value,
    derive_anchors,
    evaluate_league,
    inseason_level_by_position,
    weeks_stale,
)
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _weeks(rows):
    """rows: list of (week, snap_pct, target_share, ppr, targets, carries[, team])."""
    cols = ["week", "snap_pct", "target_share", "fantasy_points_ppr", "targets", "carries"]
    out = []
    for r in rows:
        d = dict(zip(cols, r[:6]))
        if len(r) > 6:
            d["nfl_team"] = r[6]
        out.append(d)
    return pd.DataFrame(out)


def _val(rows, *, pos="WR", cw=14, stale=0.0, prior=None):
    return compute_player_value(
        canonical_player_id="p", name="P", position=pos,
        weeks=_weeks(rows), current_week=cw, prior_projection_ppg=prior,
        stale_weeks=stale,
    )


# A strong, currently-playing player: ~18 ppg, steady ~70% snaps, played through wk13.
_STUD_ROWS = [(w, 0.72, 0.24, 18.0, 9, 0) for w in range(1, 14)]


# ---------------------------------------------------------------------------
# STEP 1 — weeks_stale primitive (recon blast-radius table, cw=14)
# ---------------------------------------------------------------------------
def test_weeks_stale_matches_blast_radius_table():
    assert weeks_stale(9, 14, set()) == 5          # Kraft (GB, no bye after wk9)
    assert weeks_stale(4, 14, {12}) == 9           # Tyreek (MIA bye wk12 excluded)
    assert weeks_stale(13, 14, {14}) == 0          # CMC (SF bye wk14) — bye, not stale
    assert weeks_stale(13, 14, {14}) == 0          # Wan'Dale (NYG bye wk14)
    assert weeks_stale(11, 14, set()) == 3         # Drake London (ATL)
    assert weeks_stale(3, 14, {8}) == 10           # James Conner (ARI bye wk8 excluded)
    assert weeks_stale(13, 14, set()) == 1         # single missed game
    assert weeks_stale(14, 14, set()) == 0         # current
    assert weeks_stale(None, 14, set()) == 0       # no history


def test_bye_weeks_from_schedule_derives_byes():
    # two teams, one week each missing → that's their bye.
    sched = pd.DataFrame([
        {"game_type": "REG", "week": 1, "home_team": "GB", "away_team": "CHI"},
        {"game_type": "REG", "week": 2, "home_team": "GB", "away_team": "MIN"},
        {"game_type": "REG", "week": 1, "home_team": "DET", "away_team": "KC"},
        # week 2: DET absent → DET bye wk2; GB plays both → GB no bye in 1..2
    ])
    byes = bye_weeks_from_schedule(sched)
    assert byes["DET"] == {2}
    assert byes["GB"] == set()


# ---------------------------------------------------------------------------
# §7.1 INJURY CRATERS — Kraft / Tyreek / London toward floor, flags gone
# ---------------------------------------------------------------------------
def test_injury_craters_value_and_kills_flag():
    # Kraft-shaped: 8 strong TE games then nothing; "falling" pre-injury → was a
    # phantom sell-high. At 5 weeks stale the value craters and the flag is gone.
    rows = [(w, 0.80, 0.20, 14.0, 7, 0) for w in range(2, 8)] + \
           [(8, 0.56, 0.16, 11.0, 6, 0), (9, 0.44, 0.12, 9.0, 5, 0)]
    fresh = _val(rows, pos="TE", stale=0.0)
    stale = _val(rows, pos="TE", stale=5.0)
    assert stale.forward_value < fresh.forward_value * 0.25     # cratered toward floor
    assert stale.sell_high is False and stale.buy_low is False  # no actionable flag
    assert stale.confidence is Confidence.INSUFFICIENT


def test_long_absence_floors_value():
    # Tyreek-shaped 10-week absence → essentially floored.
    rows = [(w, 0.85, 0.28, 17.0, 10, 0) for w in range(1, 5)]
    v = _val(rows, pos="WR", stale=9.0)
    assert v.forward_value < 5.0
    assert v.confidence is Confidence.INSUFFICIENT


# ---------------------------------------------------------------------------
# §7.2 BYE READS CORRECTLY — "22, bye, 20" is real recent form, not diluted
# ---------------------------------------------------------------------------
def test_bye_in_window_is_not_a_zero():
    # weeks 11 and 13 played (22, 20); week 12 is a bye → NO row. The played-row
    # window skips it, so recency reflects the real 22/20 form, not a 0.
    rows = [(w, 0.70, 0.22, 16.0, 8, 0) for w in range(6, 11)] + \
           [(11, 0.74, 0.24, 22.0, 10, 0), (13, 0.74, 0.24, 20.0, 10, 0)]
    v = _val(rows, pos="WR", stale=0.0)        # current (last game wk13, bye-adjusted)
    assert v.recency_ppg >= 19.0               # the 22/20, not diluted by a phantom 0
    assert v.confidence is Confidence.FULL


# ---------------------------------------------------------------------------
# §7.3 BYE-AT-EDGE UNTOUCHED — CMC/Wan'Dale (bye in the current week) full value
# ---------------------------------------------------------------------------
def test_bye_at_data_edge_is_not_penalized():
    # Played through wk13; team bye is wk14 (the current week) → stale 0 → no decay.
    fresh = _val(_STUD_ROWS, pos="RB", cw=14, stale=0.0)
    assert fresh.forward_value > 40
    assert fresh.confidence is Confidence.FULL
    # weeks_stale would compute 0 for this player given the wk14 bye:
    assert weeks_stale(13, 14, {14}) == 0


# ---------------------------------------------------------------------------
# §7.4 SINGLE-GAP TOLERATED — one missed game (stale 1) ~ unchanged
# ---------------------------------------------------------------------------
def test_single_missed_game_is_tolerated():
    fresh = _val(_STUD_ROWS, pos="WR", stale=0.0)
    one = _val(_STUD_ROWS, pos="WR", stale=1.0)
    assert one.forward_value == fresh.forward_value     # free-weeks guard → identical
    assert one.confidence is Confidence.FULL
    assert _staleness_decay(1.0) == 1.0


# ---------------------------------------------------------------------------
# §7.5 #158 GUARD — healthy, currently-playing, stable stud is UNCHANGED
# ---------------------------------------------------------------------------
def test_healthy_current_stud_unchanged_by_the_fix():
    # The non-negotiable: a current player (stale 0, no byes) is byte-identical to
    # pre-fix — staleness must not re-break the frozen calibration.
    v0 = _val(_STUD_ROWS, pos="WR", stale=0.0)
    assert v0.confidence is Confidence.FULL
    assert _staleness_decay(0.0) == 1.0
    # the decay factor is exactly 1.0, so forward_ppg/forward_value are untouched.
    assert v0.forward_ppg > 15.0 and v0.forward_value > 40


# ---------------------------------------------------------------------------
# §7.7 CONFIDENCE DAMPENS TREND — staleness rides the #170 confidence scale
# ---------------------------------------------------------------------------
def test_staleness_downgrades_confidence_and_dampens_trend():
    # A rising-usage player: fresh → buy_low + RISING flag; stale → confidence
    # downgraded so the trend factor is scaled away and no actionable flag fires.
    rising = [(w, 0.40 + 0.04 * w, 0.10 + 0.012 * w, 6.0 + w, 5, 0) for w in range(1, 8)]
    fresh = _val(rising, pos="WR", stale=0.0)
    assert fresh.buy_low is True and fresh.value_trend is ValueTrend.RISING
    stale = _val(rising, pos="WR", stale=4.0)
    assert stale.confidence is Confidence.INSUFFICIENT
    assert stale.buy_low is False                      # trend signal suppressed

    # _assess_confidence threshold ladder (games-rich but stale):
    assert _assess_confidence(8, False, 0.0)[0] is Confidence.FULL
    assert _assess_confidence(8, False, 1.0)[0] is Confidence.FULL          # free guard
    assert _assess_confidence(8, False, 2.0)[0] is Confidence.LIMITED
    assert _assess_confidence(8, False, 3.0)[0] is Confidence.INSUFFICIENT


# ---------------------------------------------------------------------------
# §4 RECONCILIATION — the decay is per-player and must NOT perturb pool anchors
# ---------------------------------------------------------------------------
def _state(specs):
    # specs: list of (pid, name, pos)
    me = TeamState("me", "Me", True,
                   tuple(RosterPlayer(p, n, pos) for p, n, pos in specs))
    return LeagueState(2025, 14, (me,))


def test_staleness_does_not_perturb_pool_anchor_derivation():
    # Two leagues identical except player X is stale in one. A healthy player Y's
    # forward_value must be IDENTICAL — the per-player decay must not leak into the
    # shared anchors (#172). Build weekly rows for both, vary only X's last week.
    def rows_for(pid, last_week, ppg, team):
        return pd.DataFrame([
            {"canonical_player_id": pid, "week": w, "snap_pct": 0.7,
             "target_share": 0.2, "fantasy_points_ppr": ppg, "targets": 8,
             "carries": 0, "nfl_team": team}
            for w in range(1, last_week + 1)
        ])

    y = rows_for("y", 13, 17.0, "AAA")                 # healthy stud, all leagues
    pool = [rows_for(f"f{i}", 13, 8.0 + i, "BBB") for i in range(6)]  # depth for anchors
    state = _state([("y", "Y", "WR")] + [(f"f{i}", f"F{i}", "WR") for i in range(6)] +
                   [("x", "X", "WR")])

    healthy = pd.concat([y, rows_for("x", 13, 16.0, "CCC")] + pool, ignore_index=True)
    injured = pd.concat([y, rows_for("x", 4, 16.0, "CCC")] + pool, ignore_index=True)
    byes = {}  # no byes → X (last wk4) is 10 weeks stale in `injured`

    v_h = evaluate_league(state, healthy, bye_weeks=byes)
    v_i = evaluate_league(state, injured, bye_weeks=byes)

    assert v_i["x"].forward_value < v_h["x"].forward_value      # X craters when stale
    assert v_i["y"].forward_value == v_h["y"].forward_value     # Y untouched → anchors stable
    # and the anchors themselves are computed off raw levels, unchanged by the decay:
    rp = {"y": "WR", "x": "WR", **{f"f{i}": "WR" for i in range(6)}}
    a_h = derive_anchors(inseason_level_by_position(healthy, rp, 14))
    a_i = derive_anchors(inseason_level_by_position(injured, rp, 14))
    # injured X lowers his OWN raw level (fewer-but-same games → same mean), but the
    # WR anchor band is driven by the pool; confirm Y's scaling is identical above.
    assert a_h["WR"][1] > 0 and a_i["WR"][1] > 0


# ---------------------------------------------------------------------------
# evaluate_league threads staleness end-to-end via injected byes
# ---------------------------------------------------------------------------
def test_evaluate_league_applies_staleness_from_injected_byes():
    rows = pd.DataFrame(
        [{"canonical_player_id": "kraft", "week": w, "snap_pct": 0.8,
          "target_share": 0.2, "fantasy_points_ppr": 14.0, "targets": 7,
          "carries": 0, "nfl_team": "GB"} for w in range(2, 10)]      # last game wk9
        + [{"canonical_player_id": "stud", "week": w, "snap_pct": 0.75,
            "target_share": 0.25, "fantasy_points_ppr": 18.0, "targets": 9,
            "carries": 0, "nfl_team": "KC"} for w in range(1, 14)]    # plays through wk13
    )
    state = _state([("kraft", "Kraft", "TE"), ("stud", "Stud", "WR")])
    byes = {"GB": set(), "KC": {10}}   # KC bye wk10 (won't matter — stud current)
    vals = evaluate_league(state, rows, bye_weeks=byes)
    assert vals["kraft"].confidence is Confidence.INSUFFICIENT       # 5 weeks stale
    assert vals["kraft"].forward_value < 5.0
    assert vals["stud"].confidence is Confidence.FULL                # current
    assert vals["stud"].forward_value > 40


# ===========================================================================
# STEEPER LONG-ABSENCE TAIL (docs/trade_value_availability_design.md follow-up):
# the #177 power=2 decay was too GENTLE — a 3-week absentee only ~halved (London
# read a still-startable 9.2 that masked The Lord's WR need). The tail-steepness
# POWER is raised so 3+ week absences floor, while the short-gap band (≤2 weeks) is
# pinned byte-identical (effective=1 → RATE·1 for ANY power). #174 gate untouched.
# ===========================================================================
def test_decay_tail_is_steeper_but_short_gaps_are_pinned():
    # Short gaps UNCHANGED from #177 (the 8c guard + the pinned 2-week point):
    assert _staleness_decay(0.0) == 1.0
    assert _staleness_decay(1.0) == 1.0                 # free-weeks guard
    assert abs(_staleness_decay(2.0) - 0.667) < 0.005   # 2-week gap == #177's gentle value
    # Long-absence tail is now STEEP (was 0.333 at stale 3 under #177's power=2):
    assert _staleness_decay(3.0) < 0.10                 # London-class → near floor
    assert _staleness_decay(3.0) < 0.333 / 3            # materially steeper than #177
    assert _staleness_decay(5.0) < 0.01                 # Kraft-class → floor
    assert _staleness_decay(10.0) < 0.001               # Tyreek-class → floor
    # strictly monotonic decreasing in the tail
    assert _staleness_decay(2.0) > _staleness_decay(3.0) > _staleness_decay(4.0)


def test_three_week_absence_floors_not_midband():
    # The headline defect: a 3-week-out stud must read near the floor, NOT a
    # still-startable mid-band value. Compare the SAME stud at stale 2 vs 3.
    two = _val(_STUD_ROWS, pos="WR", stale=2.0)
    three = _val(_STUD_ROWS, pos="WR", stale=3.0)
    assert three.forward_value < 5.0                    # near floor (London was 9.2 under #177)
    assert three.forward_value < two.forward_value * 0.2  # the tail cliff: 3w << 2w
    assert three.confidence is Confidence.INSUFFICIENT


def test_short_gap_still_barely_moves_a_stud():
    # SHORT GAP UNCHANGED guard: a 1-week absence does not decay; a 2-week absence
    # keeps its gentle #177 value (NOT dragged down by the steeper tail) and stays
    # well above the floored 3-week value.
    fresh = _val(_STUD_ROWS, pos="WR", stale=0.0)
    one = _val(_STUD_ROWS, pos="WR", stale=1.0)
    two = _val(_STUD_ROWS, pos="WR", stale=2.0)
    three = _val(_STUD_ROWS, pos="WR", stale=3.0)
    assert one.forward_value == fresh.forward_value         # no decay at 1 week
    assert two.confidence is Confidence.LIMITED             # 2w → gentle, still startable
    assert two.forward_value > 20                           # a strong stud only ~halved
    assert two.forward_value > 5 * three.forward_value      # 2-week >> floored 3-week


# ---------------------------------------------------------------------------
# THE UNLOCK (guarded end-to-end) — flooring the 3-week London deflates The Lord's
# inflated WR lineup so real acquisitions clear cond1; Fat Bastard unlocks too.
# Runs only with the real 2025 cache + DB present (skips in CI, like the demo seed).
# ---------------------------------------------------------------------------
_WEEKLY_CACHE = Path("data/cache/weekly_pbp_2025.parquet")


@pytest.mark.skipif(
    not _WEEKLY_CACHE.exists(),
    reason="real 2025 per-week data not on disk (CI) — synthetic tests cover the curve",
)
async def test_steeper_decay_unlocks_lord_and_fat_bastard_at_unchanged_gate():
    from backend.database import AsyncSessionLocal
    from backend.integrations.nfl_data import fetch_schedules
    from backend.services.trade.trade_demo_source import seed_demo_league
    from backend.services.trade.trade_proposals import (
        enumerate_candidates, evaluate_candidates)

    try:
        async with AsyncSessionLocal() as db:
            src = await seed_demo_league(db)
    except Exception as exc:  # no DB / not populated → skip, don't fail
        pytest.skip(f"demo DB unavailable: {exc}")

    state = src.get_league_state()
    byes = bye_weeks_from_schedule(fetch_schedules(2025))
    vals = evaluate_league(state, src.weekly_usage, priors=src.priors, bye_weeks=byes)

    def value_of(nm):
        for t in state.teams:
            for rp in t.roster:
                if rp.name == nm:
                    return vals[rp.canonical_player_id].forward_value
        return None

    # London (out 3) floored — NOT the #177 mid-band 9.2.
    assert value_of("Drake London") < 5.0
    # Bye-at-edge studs untouched (proves we floor injuries, not byes).
    assert value_of("Christian McCaffrey") > 90
    assert value_of("Wan'Dale Robinson") > 40

    def cleared(team_name):
        t = next(x for x in state.teams if x.team_name == team_name)
        cands = enumerate_candidates(state, vals, t.team_id)
        return evaluate_candidates(state, vals, t.team_id, cands, roster_limit=20)

    lord = cleared("The Lord")
    fatb = cleared("Fat Bastard")
    assert len(lord) >= 1        # unlocked (was 0 with London at 9.2)
    assert len(fatb) >= 1        # unlocked
    # every surfaced trade cleared the FULL unchanged #174 gate (real upgrade).
    for _, _, e in lord + fatb:
        assert e.clears is True and e.your_lineup_gain >= 5.0
