"""
Unit tests for the in-season value engine (backend/services/trade/value_engine.py).

All fixtures are FIXED, hand-built per-week lines (the shape the #149 layer
emits). The headline assertions exercise the differentiator: value follows usage
TRAJECTORY and current production, never the player's name/reputation.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.services.trade.league_state import (
    LeagueState,
    RosterPlayer,
    TeamState,
)
import backend.services.trade.value_engine as ve
from backend.services.trade.contextual import contextual_value
from backend.services.trade.lineup import LineupPlayer
from backend.services.trade.value_engine import (
    ValueTrend,
    _COMBINED_FACTOR_BOUNDS,
    _OPP_GAP_CAP,
    _PPG_ANCHORS,
    _REPLACEMENT_FLOOR,
    _TRAJECTORY_CAP,
    _bound_combined,
    _played_weeks,
    _scale_0_100,
    compute_player_value,
    derive_anchors,
    evaluate_league,
    inseason_level,
    inseason_level_by_position,
    opp_gap_factor,
    recency_ppg,
    season_ppg,
    season_ppg_by_position,
    usage_trajectory_factor,
    usage_trend,
)


def _weeks(snaps, targets, points, *, carries=None, tgts=None, start_week=1):
    """Build a per-player weekly frame from parallel lists."""
    n = len(snaps)
    carries = carries or [0] * n
    tgts = tgts if tgts is not None else [0] * n
    return pd.DataFrame({
        "week": list(range(start_week, start_week + n)),
        "snap_pct": snaps,
        "target_share": targets,
        "fantasy_points_ppr": points,
        "targets": tgts,
        "carries": carries,
    })


# ---------------------------------------------------------------------------
# Trend math — last-2 vs prior-3 direction on a constructed series
# ---------------------------------------------------------------------------
def test_usage_trend_rising_when_recent_two_exceed_prior_three():
    df = _played_weeks(
        _weeks([0.40, 0.45, 0.50, 0.80, 0.90], [0.08, 0.10, 0.12, 0.22, 0.26], [5] * 5),
        current_week=5,
    )
    recent, prior, delta, trend = usage_trend(df)
    assert trend is ValueTrend.RISING
    assert delta > 0 and recent > prior


def test_usage_trend_falling_when_recent_two_below_prior_three():
    df = _played_weeks(
        _weeks([0.90, 0.85, 0.80, 0.50, 0.45], [0.28, 0.26, 0.24, 0.12, 0.10], [10] * 5),
        current_week=5,
    )
    _, _, delta, trend = usage_trend(df)
    assert trend is ValueTrend.FALLING
    assert delta < 0


def test_usage_trend_stable_when_flat():
    df = _played_weeks(
        _weeks([0.6] * 5, [0.15] * 5, [10] * 5), current_week=5,
    )
    _, _, delta, trend = usage_trend(df)
    assert trend is ValueTrend.STABLE
    assert delta == pytest.approx(0.0)


def test_played_weeks_respects_current_week_anchor():
    """Weeks after the anchor are excluded — engine is week-agnostic."""
    df = _played_weeks(_weeks([0.6] * 6, [0.15] * 6, [10] * 6), current_week=4)
    assert df["week"].max() == 4
    assert len(df) == 4


# ---------------------------------------------------------------------------
# Buy-low: rising usage
# ---------------------------------------------------------------------------
def test_rising_usage_flags_buy_low():
    """A WR whose snap% + target share climb over the last two weeks is BUY-LOW,
    even on still-modest points."""
    weeks = _weeks(
        snaps=[0.40, 0.45, 0.50, 0.80, 0.90],
        targets=[0.08, 0.10, 0.12, 0.22, 0.26],
        points=[5, 6, 7, 12, 14],
        tgts=[4, 5, 6, 11, 13],
    )
    v = compute_player_value(
        canonical_player_id="rise", name="Rising WR", position="WR",
        weeks=weeks, current_week=5,
    )
    assert v.value_trend is ValueTrend.RISING
    assert v.buy_low is True
    assert v.sell_high is False
    assert "rising" in v.why


def test_opportunity_gap_flags_buy_low_on_high_volume_low_output():
    """Heavy volume but suppressed output → production should catch up → buy."""
    weeks = _weeks(
        snaps=[0.85] * 5, targets=[0.24] * 5, points=[5, 4, 6, 5, 4],
        tgts=[10, 11, 10, 12, 11],
    )
    v = compute_player_value(
        canonical_player_id="vol", name="Volume WR", position="WR",
        weeks=weeks, current_week=5,
    )
    assert v.opportunity_gap <= -4.0
    assert v.buy_low is True


# ---------------------------------------------------------------------------
# Sell-high: declining usage on a name brand + the name-bias guard
# ---------------------------------------------------------------------------
def test_declining_name_brand_flags_sell_high_and_discounts_reputation():
    """A reputation player (high preseason prior) whose role is decaying is
    SELL-HIGH, and the name-bias guard discounts the prior so reputation cannot
    prop the forward value back up."""
    weeks = _weeks(
        snaps=[0.90, 0.80, 0.55, 0.50],
        targets=[0.28, 0.24, 0.13, 0.11],
        points=[18, 16, 9, 8],
        tgts=[9, 8, 4, 4],
    )
    v = compute_player_value(
        canonical_player_id="fade", name="Star WR", position="WR",
        weeks=weeks, current_week=4,
        prior_projection_ppg=19.0,   # big preseason reputation
    )
    assert v.value_trend is ValueTrend.FALLING
    assert v.sell_high is True
    assert v.name_bias_guard_applied is True
    assert v.prior_weight < 0.15                  # reputation heavily discounted
    # A 19-ppg reputation would scale to ~73/100; the decayed role pins it far lower.
    assert v.forward_value < 40
    assert "down-weighted" in v.why


def test_unsustainable_hot_scoring_flags_sell_high():
    """High points on low volume + falling usage = TD-variance → sell."""
    weeks = _weeks(
        snaps=[0.70, 0.65, 0.45, 0.40],
        targets=[0.18, 0.16, 0.08, 0.07],
        points=[8, 9, 19, 20],
        tgts=[6, 5, 3, 3],
    )
    v = compute_player_value(
        canonical_player_id="hot", name="Hot WR", position="WR",
        weeks=weeks, current_week=4,
    )
    assert v.value_trend is ValueTrend.FALLING
    assert v.opportunity_gap >= 4.0
    assert v.sustainable is False
    assert v.sell_high is True


# ---------------------------------------------------------------------------
# Prior vs in-season CONFLICT — value follows the in-season data
# ---------------------------------------------------------------------------
def test_high_prior_but_weak_inseason_value_follows_inseason():
    """Preseason says stud (prior 20 ppg → would scale ~80/100), but six weeks of
    middling usage/production say otherwise. With a full in-season sample the
    prior washes out and forward_value tracks the in-season reality."""
    weeks = _weeks(
        snaps=[0.50] * 6, targets=[0.10] * 6,
        points=[11, 12, 10, 11, 12, 11], tgts=[5] * 6,
    )
    v = compute_player_value(
        canonical_player_id="bust", name="Preseason Stud", position="WR",
        weeks=weeks, current_week=6,
        prior_projection_ppg=20.0,
    )
    assert v.prior_weight == pytest.approx(0.0)     # 6 games ≥ full-in-season
    # in-season ~11 ppg → low score, nowhere near the prior-implied ~80.
    assert v.forward_value < 40
    # LEVEL blends recent form with the season-to-date baseline (~11), then the
    # opp-gap factor lightly discounts (scoring ~11 above the ~7 volume-implied),
    # so forward stays anchored to in-season reality (~10.7), not the 20-ppg prior.
    assert v.forward_ppg == pytest.approx(10.7, abs=0.7)


# ---------------------------------------------------------------------------
# Name-bias guard as a real, testable code path: value is name-independent
# ---------------------------------------------------------------------------
def test_value_is_independent_of_player_name():
    """Identical usage/production with different names/ids → identical value.
    Proves value derives from the data, never the name."""
    weeks_a = _weeks([0.6, 0.62, 0.7, 0.75], [0.18, 0.2, 0.22, 0.24], [12, 13, 15, 16])
    weeks_b = weeks_a.copy()
    common = dict(position="WR", weeks=weeks_a, current_week=4, prior_projection_ppg=14.0)
    famous = compute_player_value(canonical_player_id="x", name="Superstar Famous", **{**common, "weeks": weeks_a})
    nobody = compute_player_value(canonical_player_id="y", name="Anonymous Scrub", **{**common, "weeks": weeks_b})
    assert famous.forward_value == nobody.forward_value
    assert famous.value_trend == nobody.value_trend
    assert famous.buy_low == nobody.buy_low and famous.sell_high == nobody.sell_high


def test_no_inseason_data_falls_back_to_prior_only():
    v = compute_player_value(
        canonical_player_id="z", name="Injured", position="RB",
        weeks=_weeks([], [], []), current_week=5, prior_projection_ppg=16.0,
    )
    assert v.games_played == 0
    assert v.value_trend is ValueTrend.STABLE
    assert v.forward_ppg == pytest.approx(16.0)


# ---------------------------------------------------------------------------
# League-level convenience
# ---------------------------------------------------------------------------
def test_evaluate_league_values_every_rostered_player():
    weekly = pd.concat([
        _weeks([0.4, 0.5, 0.6, 0.8, 0.9], [0.1, 0.12, 0.14, 0.22, 0.26], [6, 7, 8, 13, 15]).assign(canonical_player_id="rise"),
        _weeks([0.9, 0.85, 0.8, 0.5, 0.45], [0.28, 0.26, 0.24, 0.12, 0.1], [18, 16, 14, 9, 8]).assign(canonical_player_id="fade"),
    ], ignore_index=True)
    state = LeagueState(
        season=2025, week=5,
        teams=(
            TeamState("t1", "Mine", is_me=True, roster=(
                RosterPlayer("rise", "Rising WR", "WR"),
            )),
            TeamState("t2", "Theirs", is_me=False, roster=(
                RosterPlayer("fade", "Fading WR", "WR"),
            )),
        ),
    )
    values = evaluate_league(state, weekly)
    assert set(values) == {"rise", "fade"}
    assert values["rise"].value_trend is ValueTrend.RISING
    assert values["rise"].buy_low is True
    assert values["fade"].value_trend is ValueTrend.FALLING
    assert values["fade"].sell_high is True


# ---------------------------------------------------------------------------
# LEVEL calibration — value is anchored to season body of work, not last 3 weeks
# ---------------------------------------------------------------------------
def test_chase_class_strong_season_mild_recent_dip_is_not_crushed():
    """A strong-season, stable-usage player whose last 3 weeks dipped must read
    HIGH-but-not-elite — the season body of work pulls the level up. Recency-only
    (the old behavior) would collapse this to near-replacement (~7/100)."""
    weeks = _weeks(
        snaps=[0.8] * 8, targets=[0.22] * 8,
        points=[22, 22, 22, 22, 22, 9, 9, 9],   # ~17 season, ~9 last-3 (sharp dip)
        tgts=[8] * 8,
    )
    v = compute_player_value(
        canonical_player_id="chaseish", name="Strong WR", position="WR",
        weeks=weeks, current_week=8,
    )
    assert v.value_trend is ValueTrend.STABLE          # usage flat — it's scoring variance
    # The season baseline lifts the level above the recency-only result…
    assert v.forward_ppg > v.recency_ppg + 2
    # …so the value is materially higher than the buggy recency-only ~7 and NOT ~25,
    # but still below the season-only ceiling (~61).
    assert 28 < v.forward_value < 55


def test_genuine_decline_still_reads_low():
    """Weak season AND weak recent → still low. We anchored to the season, we did
    not inflate everyone."""
    weeks = _weeks(
        snaps=[0.3] * 8, targets=[0.05] * 8,
        points=[6, 7, 5, 6, 7, 5, 6, 5],          # weak throughout
        tgts=[3] * 8,
    )
    v = compute_player_value(
        canonical_player_id="weak", name="Fungible WR", position="WR",
        weeks=weeks, current_week=8,
    )
    assert v.forward_value < 12                         # near/below replacement


def test_hot_but_unsustainable_is_not_credited_at_the_hot_rate():
    """A short hot streak above the season level on falling usage is NOT fully
    credited: the season baseline tempers it AND the opp-gap factor (which now
    SUBSUMES the old unsustainable-hot regression) discounts it, so the level lands
    well below the recent hot form."""
    weeks = _weeks(
        snaps=[0.70, 0.65, 0.45, 0.40], targets=[0.18, 0.16, 0.08, 0.07],
        points=[8, 9, 19, 20],                    # ~17.5 hot last-3 vs ~14 season
        tgts=[6, 5, 3, 3],
    )
    v = compute_player_value(
        canonical_player_id="hot2", name="Hot WR", position="WR",
        weeks=weeks, current_week=4,
    )
    assert v.sustainable is False                       # unsustainable-hot still fires
    assert v.sell_high is True
    # level is tempered well below the ~17.5 recent hot form
    assert v.forward_ppg < v.recency_ppg - 3


# ---------------------------------------------------------------------------
# Pool-derived positional anchors (replacement / elite from the real pool)
# ---------------------------------------------------------------------------
def _pool(top, n, step):
    """A descending season-ppg list of n players from `top` by `step`."""
    return [round(top - i * step, 1) for i in range(n)]


def test_derive_anchors_qb_replacement_is_higher_than_wr():
    """The whole point: QB replacement (everyone starts 1) is legitimately higher
    than WR replacement (deep position), so anchors reflect real positional depth."""
    pools = {
        "QB": _pool(27, 24, 0.8),   # 24 QBs, high floor
        "RB": _pool(26, 60, 0.32),
        "WR": _pool(25, 72, 0.27),  # deep WR pool
        "TE": _pool(18, 24, 0.45),
    }
    a = derive_anchors(pools, teams=12)
    assert a["QB"][0] > a["WR"][0]          # QB replacement higher than WR (the point)
    for pos in ("QB", "RB", "WR", "TE"):
        assert a[pos][1] > a[pos][0]        # elite > replacement everywhere
        assert a[pos] != _PPG_ANCHORS[pos]  # actually derived, not the fallback


def test_derive_anchors_falls_back_when_pool_too_sparse():
    a = derive_anchors({"QB": [22, 19, 16]}, teams=12)  # 3 QBs « cutoff+band
    assert a["QB"] == _PPG_ANCHORS["QB"]                # documented fallback


def test_cross_position_same_relative_standing_reads_identically():
    """A player at the same fractional position in his position's band reads the
    SAME value regardless of position — the QB-vs-WR inconsistency fixed."""
    anchors = {"QB": (16.0, 24.0), "WR": (9.0, 21.0)}
    qb_mid = 16.0 + 0.5 * (24.0 - 16.0)   # 20.0
    wr_mid = 9.0 + 0.5 * (21.0 - 9.0)     # 15.0
    # Soft floor: the startable band is FLOOR..100, so the mid-band point is
    # FLOOR + 0.5·(100−FLOOR) = 55.0 — still IDENTICAL cross-position (the point).
    assert _scale_0_100(qb_mid, "QB", anchors) == 55.0
    assert _scale_0_100(wr_mid, "WR", anchors) == 55.0  # identical, cross-position


def test_lamar_class_startable_qb_not_crushed_under_derived_anchor():
    """A genuinely startable mid QB (forward above the derived QB replacement)
    reads a sane mid value — not collapsed to ~12/near-zero like the hardcoded
    QB anchor did to a 15-16 ppg QB."""
    anchors = {"QB": (16.0, 21.0), "RB": (6, 22), "WR": (9, 19), "TE": (9, 15)}
    v = compute_player_value(
        canonical_player_id="qb", name="Mid QB", position="QB",
        weeks=_weeks([0.95] * 6, [0.0] * 6, [19] * 6), current_week=6,
        anchors=anchors,
    )
    assert v.forward_value > 30          # sane starter, not near-zero


def test_amon_ra_class_played_but_down_wr_stays_mid_scale():
    anchors = {"QB": (16, 21), "RB": (6, 22), "WR": (9.0, 19.0), "TE": (9, 15)}
    v = compute_player_value(
        canonical_player_id="wr", name="Down WR", position="WR",
        weeks=_weeks([0.8] * 6, [0.2] * 6, [15.8] * 6), current_week=6,
        anchors=anchors,
    )
    assert 40 < v.forward_value < 85     # mid-scale, not broken by derivation


def test_stud_stays_high_and_genuine_low_stays_low_under_derived_anchors():
    anchors = {"QB": (16, 21), "RB": (6.0, 22.0), "WR": (9, 19), "TE": (9, 15)}
    stud = compute_player_value(
        canonical_player_id="cmc", name="Stud RB", position="RB",
        weeks=_weeks([0.9] * 6, [0.2] * 6, [24] * 6), current_week=6, anchors=anchors,
    )
    assert stud.forward_value >= 95      # elite stays near the top
    weak = compute_player_value(
        canonical_player_id="weak", name="Deep RB", position="RB",
        weeks=_weeks([0.2] * 6, [0.03] * 6, [5] * 6), current_week=6, anchors=anchors,
    )
    assert weak.forward_value < 10       # below replacement stays low


# ===========================================================================
# SOFT FLOOR (replacement → FLOOR, not 0) — restore ordering in the below-
# replacement band so producing players aren't an indistinguishable 0.
# ===========================================================================
def test_producing_below_replacement_player_is_not_zero():
    """§ headline: a Jefferson-class WR (7.9 ppg, just below the 8.7 anchor) reads
    a small POSITIVE value (~9), not 0. A truly-zero-production player still 0."""
    anchors = {"WR": (8.7, 19.2)}
    jj = _scale_0_100(7.9, "WR", anchors)
    assert jj > 0
    assert jj == pytest.approx(9.1, abs=0.3)            # ~9, not the old hard 0
    assert _scale_0_100(0.0, "WR", anchors) == 0.0       # no production → still 0
    assert _scale_0_100(-3.0, "WR", anchors) == 0.0      # negative guarded → 0


def test_below_replacement_band_preserves_rank():
    """The ordering info the hard clamp destroyed is restored: three below-
    replacement WRs at distinct ppg read distinct, monotonic, positive values."""
    anchors = {"WR": (8.0, 23.0)}
    v7, v4, v2 = (_scale_0_100(p, "WR", anchors) for p in (7.0, 4.0, 2.0))
    assert v7 > v4 > v2 > 0                              # distinct + monotonic + positive
    assert all(x < _REPLACEMENT_FLOOR for x in (v7, v4, v2))   # all in the sub-replacement band


def test_above_replacement_band_ordered_into_floor_to_100():
    """The startable band still works, just compressed into FLOOR..100: elite ~100,
    a just-above-replacement player ~FLOOR, monotonic between."""
    anchors = {"RB": (8.0, 24.0)}
    elite = _scale_0_100(24.0, "RB", anchors)
    mid = _scale_0_100(16.0, "RB", anchors)
    just_above = _scale_0_100(8.1, "RB", anchors)
    assert elite == 100.0
    assert just_above == pytest.approx(_REPLACEMENT_FLOOR, abs=1.0)   # ~FLOOR at replacement
    assert elite > mid > just_above


def test_weak_rbs_are_distinguishable_and_carry_tradeable_value():
    """§ the payoff (contextual un-corruption): a Your-Squad-shaped set of weak RBs
    reads small-positive + ORDERED (not an all-0 blob), so a roster of weak RBs is
    distinguishable from a roster of NO RBs, and a below-replacement player is no
    longer worth literally 'nothing' to trade."""
    anchors = {"QB": (16, 24), "RB": (8.0, 24.0), "WR": (8, 23), "TE": (9, 15)}

    def rb(pid, ppg):
        return compute_player_value(
            canonical_player_id=pid, name=pid, position="RB",
            weeks=_weeks([0.5] * 4, [0.05] * 4, [ppg] * 4, carries=[8] * 4),
            current_week=4, anchors=anchors,
        )
    a, b, c = rb("a", 7), rb("b", 4), rb("c", 2)
    assert a.forward_value > b.forward_value > c.forward_value > 0   # ordered, all nonzero

    # Each weak RB still carries a small, ranked contextual value to an RB-needy
    # roster (no RBs) — the better weak RB is worth more; neither reads as "nothing".
    needy = [LineupPlayer("q", "QB", 60), LineupPlayer("w1", "WR", 60),
             LineupPlayer("w2", "WR", 55), LineupPlayer("w3", "WR", 50), LineupPlayer("te", "TE", 40)]
    cv_a = contextual_value(LineupPlayer("a", "RB", a.forward_value), needy)
    cv_c = contextual_value(LineupPlayer("c", "RB", c.forward_value), needy)
    assert cv_a > cv_c > 0


def test_compute_player_value_without_anchors_uses_hardcoded_fallback():
    """Direct calls (no anchors) keep the pre-derivation hardcoded behavior — so
    the #158 calibration tests above are unaffected."""
    v = compute_player_value(
        canonical_player_id="r", name="Reg WR", position="WR",
        weeks=_weeks([0.8] * 6, [0.2] * 6, [15.8] * 6), current_week=6,
    )
    # hardcoded WR (8, 23), soft floor: FLOOR + (15.8-8)/15*(100-FLOOR) ≈ 56.8
    assert v.forward_value == pytest.approx(56.8, abs=1.0)


# ===========================================================================
# ANCHOR BASIS FIX — derive anchors from the recency-blended in-season LEVEL
# (the basis forward_value scales), not raw season_ppg. Fixes the QB mismatch:
# a QB whose forward ran below his season ppg was measured against a season-
# derived replacement and read below it despite being a clear starter.
# ===========================================================================
def _qb_pool_weekly(n=18, top_a=24.0, step=1.0, dip=8.0):
    """n QBs, each 5 early weeks at A then 3 recent weeks at A-dip (a recent dip,
    so the in-season LEVEL runs below season ppg). Flat QB snaps (target≈0)."""
    frames = []
    roster = []
    for i in range(n):
        a = top_a - i * step
        pts = [a] * 5 + [a - dip] * 3
        w = _weeks([0.95] * 8, [0.0] * 8, pts).assign(canonical_player_id=f"qb{i}")
        frames.append(w)
        roster.append(RosterPlayer(f"qb{i}", f"QB{i}", "QB"))
    return pd.concat(frames, ignore_index=True), roster


def test_inseason_level_is_anchor_independent_no_circularity():
    """The anchor basis (inseason_level) depends ONLY on production — recency_ppg
    + season_ppg — never on the anchors or forward_value, so derive_anchors has no
    circular reference."""
    df = _played_weeks(_weeks([0.8] * 6, [0.2] * 6, [10, 12, 11, 9, 8, 7]), current_week=6)
    assert inseason_level(df) == pytest.approx(round(0.5 * recency_ppg(df) + 0.5 * season_ppg(df), 2))
    assert inseason_level(_played_weeks(_weeks([], [], []), current_week=6)) == 0.0
    # it takes no anchors argument and references no scaling — purely production.


def test_anchor_basis_is_level_not_season():
    """With a recent dip, the LEVEL-basis QB anchor lands BELOW the season-basis
    anchor — measuring replacement/elite in the same units as the scaled value."""
    weekly, roster = _qb_pool_weekly()
    rp = {r.canonical_player_id: "QB" for r in roster}
    season_anchor = derive_anchors(season_ppg_by_position(weekly, rp))["QB"]
    level_anchor = derive_anchors(inseason_level_by_position(weekly, rp, current_week=8))["QB"]
    assert level_anchor[0] < season_anchor[0]      # replacement re-based down to forward
    assert level_anchor[1] < season_anchor[1]      # elite too


def test_starting_qb_reads_as_starter_under_level_basis():
    """HEADLINE: a mid/upper QB whose forward dipped below his season ppg reads as a
    real startable value under the level-basis anchor, where under the season-basis
    anchor (the bug) it was pinned near/below the floor."""
    weekly, roster = _qb_pool_weekly()
    rp = {r.canonical_player_id: "QB" for r in roster}
    state = LeagueState(season=2025, week=8,
                        teams=(TeamState("me", "Me", True, tuple(roster)),))
    fixed = evaluate_league(state, weekly)                       # level-basis (the fix)

    season_anchors = derive_anchors(season_ppg_by_position(weekly, rp))
    # a representative upper-mid QB (rank 3 by production)
    qb_id = "qb2"
    qb_weeks = weekly[weekly["canonical_player_id"] == qb_id]
    buggy = compute_player_value(
        canonical_player_id=qb_id, name="QB", position="QB",
        weeks=qb_weeks, current_week=8, anchors=season_anchors,   # season-basis (the bug)
    )
    new_fv = fixed[qb_id].forward_value
    assert new_fv > buggy.forward_value                          # the fix lifts it
    assert new_fv > 2 * _REPLACEMENT_FLOOR                       # reads as a real starter, not floor


def test_cross_position_consistency_restored_for_qb():
    """#160 goal: a player at the same fractional standing in his position's band
    reads the SAME value cross-position. With level-basis anchors, a top QB and a
    top RB both read ~100; mid-band reads ~55 either way — QB no longer alone."""
    qb_anchor, rb_anchor = (12.0, 20.0), (6.0, 22.0)
    # top of band → ~100 both; mid of band → ~55 both
    assert _scale_0_100(20.0, "QB", {"QB": qb_anchor}) == _scale_0_100(22.0, "RB", {"RB": rb_anchor}) == 100.0
    qb_mid = 12.0 + 0.5 * (20.0 - 12.0)
    rb_mid = 6.0 + 0.5 * (22.0 - 6.0)
    assert _scale_0_100(qb_mid, "QB", {"QB": qb_anchor}) == _scale_0_100(rb_mid, "RB", {"RB": rb_anchor}) == 55.0


def test_other_positions_still_sane_under_level_basis():
    """The basis change didn't break the positions that already worked: a derived
    level-basis RB anchor still has elite > replacement, a stud reads top, a
    replacement-level RB reads near the floor."""
    # 40 RBs spanning a real spread (deep enough to derive, cutoff 30 + band 5).
    frames, roster = [], []
    for i in range(40):
        ppg = 24.0 - i * 0.55
        frames.append(_weeks([0.7] * 6, [0.05] * 6, [ppg] * 6).assign(canonical_player_id=f"rb{i}"))
        roster.append(RosterPlayer(f"rb{i}", f"RB{i}", "RB"))
    weekly = pd.concat(frames, ignore_index=True)
    rp = {r.canonical_player_id: "RB" for r in roster}
    repl, elite = derive_anchors(inseason_level_by_position(weekly, rp, current_week=6))["RB"]
    assert elite > repl
    state = LeagueState(2025, 6, (TeamState("me", "Me", True, tuple(roster)),))
    vals = evaluate_league(state, weekly)
    assert vals["rb0"].forward_value >= 95          # stud near the top
    assert vals["rb39"].forward_value < _REPLACEMENT_FLOOR   # deep RB near/below floor


def test_season_ppg_by_position_groups_rostered_players():
    weekly = pd.concat([
        _weeks([0.8] * 4, [0.2] * 4, [20] * 4).assign(canonical_player_id="a"),
        _weeks([0.8] * 4, [0.2] * 4, [10] * 4).assign(canonical_player_id="b"),
    ], ignore_index=True)
    pools = season_ppg_by_position(weekly, {"a": "QB", "b": "QB"})
    assert sorted(pools["QB"]) == [10.0, 20.0]


# ===========================================================================
# USAGE-TRAJECTORY + OPP-GAP wiring (docs/trade_value_trajectory_design.md §8).
# The paired safety property: the differentiator MOVES value (Burrow flips),
# while a stable-usage stud and a thin/cross-team trend do NOT whipsaw.
# ===========================================================================
def _fv(pid, pos, weeks):
    return compute_player_value(
        canonical_player_id=pid, name=pid, position=pos, weeks=weeks, current_week=weeks["week"].max(),
    )


def test_burrow_henderson_trade_flips_with_trajectory_wiring(monkeypatch):
    """§8.1 HEADLINE: give a RISING buy-low QB (Burrow) for a FALLING sell-high RB
    (Henderson). Level-only the higher-level Henderson is a 'you win'; once the
    trajectory + opp-gap factors move value, Burrow lifts and Henderson discounts,
    and the verdict FLIPS — buy-high/sell-low no longer scores as a win."""
    burrow = _weeks([0.64, 0.66, 0.68, 0.95, 1.0], [0, 0, 0, 0, 0], [16, 17, 17, 20, 21])
    hend = _weeks([0.62, 0.60, 0.58, 0.40, 0.36], [0.10, 0.10, 0.09, 0.06, 0.05],
                  [18, 17, 16, 15, 14], tgts=[4, 4, 3, 2, 2], carries=[14, 13, 12, 8, 7])

    # LEVEL-ONLY baseline (factors disabled).
    monkeypatch.setattr(ve, "_TRAJECTORY_COEFFICIENT", 0.0)
    monkeypatch.setattr(ve, "_OPP_GAP_WEIGHT", 0.0)
    b0, h0 = _fv("burrow", "QB", burrow), _fv("hend", "RB", hend)
    delta_level = h0.forward_value - b0.forward_value
    assert delta_level > 5          # level-only: clearly "you win" getting Henderson

    # Conservative defaults restored — the differentiator moves value.
    monkeypatch.undo()
    b1, h1 = _fv("burrow", "QB", burrow), _fv("hend", "RB", hend)
    assert b1.value_trend is ValueTrend.RISING and b1.buy_low
    assert h1.value_trend is ValueTrend.FALLING and h1.sell_high
    assert b1.forward_value > b0.forward_value      # rising buy-low LIFTS
    assert h1.forward_value < h0.forward_value      # falling sell-high DISCOUNTS
    delta_factors = h1.forward_value - b1.forward_value
    assert delta_factors < delta_level              # verdict moved the right way
    assert delta_factors < 0                         # ...and FLIPPED — no longer "you win"


def test_stable_usage_stud_in_a_scoring_dip_stays_put(monkeypatch):
    """§8.2 #158 GUARD (non-negotiable): a high-value, STABLE-usage player in a
    couple low-scoring weeks barely moves — the factors key on USAGE, not scoring,
    so a scoring dip doesn't whipsaw the value."""
    weeks = _weeks([0.85] * 7, [0.24] * 7, [20, 21, 20, 22, 21, 12, 11], tgts=[9] * 7)
    monkeypatch.setattr(ve, "_TRAJECTORY_COEFFICIENT", 0.0)
    monkeypatch.setattr(ve, "_OPP_GAP_WEIGHT", 0.0)
    base = _fv("stud", "WR", weeks).forward_value
    monkeypatch.undo()
    factored = _fv("stud", "WR", weeks)
    assert factored.value_trend is ValueTrend.STABLE
    assert factored.forward_value == pytest.approx(base, rel=0.05)   # essentially unchanged


def test_team_change_trend_does_not_whipsaw_value(monkeypatch):
    """§8.3 CONFIDENCE GUARD (Lamar safety): a falling trend across a TEAM CHANGE
    is a cross-offense (wrong) signal → the confidence guard suppresses the value
    adjustment entirely, so a returning/moved player's value is not whipsawed."""
    weeks = _weeks([0.9, 0.85, 0.55, 0.50], [0.28, 0.24, 0.13, 0.11], [18, 16, 9, 8], tgts=[9, 8, 4, 4])
    weeks["nfl_team"] = ["AAA", "AAA", "BBB", "BBB"]      # changed mid-window
    monkeypatch.setattr(ve, "_TRAJECTORY_COEFFICIENT", 0.0)
    monkeypatch.setattr(ve, "_OPP_GAP_WEIGHT", 0.0)
    base = _fv("moved", "WR", weeks).forward_value
    monkeypatch.undo()
    factored = _fv("moved", "WR", weeks).forward_value
    assert factored == base          # team change → scale 0 → no adjustment despite the trend


def test_opp_gap_under_producer_lifts_value(monkeypatch):
    """§8.4: heavy volume, suppressed output → lift toward volume-implied (buy-low).
    (Points kept above positional replacement so the lift isn't floored at 0.)"""
    weeks = _weeks([0.85] * 5, [0.24] * 5, [10, 9, 11, 10, 9], tgts=[13, 12, 14, 13, 12])
    monkeypatch.setattr(ve, "_OPP_GAP_WEIGHT", 0.0)
    monkeypatch.setattr(ve, "_TRAJECTORY_COEFFICIENT", 0.0)
    base = _fv("under", "WR", weeks).forward_value
    monkeypatch.undo()
    assert _fv("under", "WR", weeks).forward_value > base


def test_opp_gap_over_producer_discounts_value(monkeypatch):
    """§8.5: low volume, high (TD-driven) output → discount toward volume-implied."""
    weeks = _weeks([0.45] * 5, [0.07] * 5, [16, 17, 18, 16, 17], tgts=[3, 2, 3, 2, 3])
    monkeypatch.setattr(ve, "_OPP_GAP_WEIGHT", 0.0)
    monkeypatch.setattr(ve, "_TRAJECTORY_COEFFICIENT", 0.0)
    base = _fv("over", "WR", weeks).forward_value
    monkeypatch.undo()
    assert _fv("over", "WR", weeks).forward_value < base


def test_unsustainable_hot_still_discounts_via_opp_gap(monkeypatch):
    """§8.6: the old falling/over-producing `unsustainable_hot` case is preserved —
    still flagged sell-high AND still discounted, now via the general opp-gap factor
    (not the removed direct regression)."""
    weeks = _weeks([0.70, 0.65, 0.45, 0.40], [0.18, 0.16, 0.08, 0.07], [8, 9, 19, 20], tgts=[6, 5, 3, 3])
    monkeypatch.setattr(ve, "_OPP_GAP_WEIGHT", 0.0)
    monkeypatch.setattr(ve, "_TRAJECTORY_COEFFICIENT", 0.0)
    base = _fv("hot", "WR", weeks).forward_value
    monkeypatch.undo()
    v = _fv("hot", "WR", weeks)
    assert v.sustainable is False and v.sell_high is True   # flag preserved
    assert v.forward_value < base                            # still discounted


def test_factors_are_bounded_confidence_scaled_and_qb_guarded():
    """§8.7: caps clamp extreme swings; QB/no-volume skips opp-gap; insufficient/
    team-change scale → 1.0; the combined product is bounded."""
    # trajectory cap (symmetric)
    assert usage_trajectory_factor(1.0, 1.0) == pytest.approx(1 + _TRAJECTORY_CAP)
    assert usage_trajectory_factor(-1.0, 1.0) == pytest.approx(1 - _TRAJECTORY_CAP)
    # opp-gap cap (symmetric) + QB/no-volume guard
    assert opp_gap_factor(100.0, 10.0, 1.0) == pytest.approx(1 - _OPP_GAP_CAP)
    assert opp_gap_factor(-100.0, 10.0, 1.0) == pytest.approx(1 + _OPP_GAP_CAP)
    assert opp_gap_factor(50.0, 0.0, 1.0) == 1.0          # no volume measured → no adjustment
    # confidence scale 0 (insufficient / team change) → untouched
    assert usage_trajectory_factor(0.2, 0.0) == 1.0
    # combined product clamped both directions
    lo, hi = _COMBINED_FACTOR_BOUNDS
    assert _bound_combined(0.5) == lo
    assert _bound_combined(1.9) == hi
