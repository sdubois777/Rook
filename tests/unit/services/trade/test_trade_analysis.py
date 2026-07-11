"""
Deterministic trade-verdict tests (backend/services/trade/trade_analysis.py).

Pure: build LeagueState + InSeasonValue fixtures directly and assert the engine-
grounded verdict — lopsided reads lopsided, thin/team-change data hedges, the
winner follows forward_value (not name), and the roster guard fires only on a
net-player overflow. No DB, no LLM.
"""
from __future__ import annotations

import pytest

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend
from backend.services.trade.trade_analysis import (
    TradeValidationError,
    analyze_trade,
    validate_trade,
)


def _iv(pid, name, fv, *, conf=Confidence.FULL, trend=ValueTrend.STABLE, reason="", pos="WR"):
    return InSeasonValue(
        canonical_player_id=pid, name=name, position=pos, forward_value=fv,
        value_trend=trend, buy_low=False, sell_high=False, why="usage signal",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv / 5, expected_ppg=fv / 5, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv / 5, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=conf,
        confidence_reason=reason,
    )


def _two_team_state(my_players, opp_players):
    me = TeamState("me", "Me", True, tuple(my_players))
    opp = TeamState("opp", "Opp", False, tuple(opp_players))
    return LeagueState(2025, 14, (me, opp))


# ---------------------------------------------------------------------------
# Verdict direction / magnitude
# ---------------------------------------------------------------------------
def test_lopsided_trade_reads_lopsided():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Get", "WR")])
    values = {"g": _iv("g", "Give", 20), "x": _iv("x", "Get", 90)}
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    # value_delta is league-local VOR RESCALED to 0-100 (points above the WR replacement
    # anchor 8.0, sparse pool → fallback): get raw 18−8=10 → rescale 67.0, give 4<8→0.
    assert a.value_delta == 67.0
    assert a.winner == "you"
    assert a.fairness == "lopsided you"
    assert a.hedged is False


def test_even_trade_reads_fair():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Get", "WR")])
    values = {"g": _iv("g", "Give", 50), "x": _iv("x", "Get", 52)}
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    assert a.winner == "even"
    assert a.fairness == "fair"


# ---------------------------------------------------------------------------
# Confidence hedging — thin / team-change data never reads "lopsided"
# ---------------------------------------------------------------------------
def test_team_change_player_hedges_a_would_be_blowout():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Cooks", "WR")])
    values = {
        "g": _iv("g", "Give", 20),
        "x": _iv("x", "Cooks", 90, conf=Confidence.LIMITED,
                 reason="team change within last-5 window — cross-team share denominator"),
    }
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    # delta of +70 would be lopsided, but the team-change player forces a hedge.
    assert a.fairness == "lean you"        # downgraded from lopsided
    assert a.hedged is True
    assert a.confidence == "limited"
    assert "Cooks" in a.hedge_reason and "team change" in a.hedge_reason


def test_insufficient_player_hedges():
    state = _two_team_state([RosterPlayer("g", "Give", "WR")], [RosterPlayer("x", "Sparse", "WR")])
    values = {
        "g": _iv("g", "Give", 20),
        "x": _iv("x", "Sparse", 90, conf=Confidence.INSUFFICIENT, reason="only 1 played game(s) — no trend"),
    }
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    assert a.hedged is True
    assert a.fairness == "lean you"
    assert a.confidence == "insufficient"


# ---------------------------------------------------------------------------
# Name-bias — verdict follows forward_value, never the name
# ---------------------------------------------------------------------------
def test_verdict_follows_engine_value_not_reputation():
    """A famous name with LOW engine value loses to an unknown with HIGH value."""
    state = _two_team_state(
        [RosterPlayer("g", "Superstar Famous", "WR")],
        [RosterPlayer("x", "Anonymous Scrub", "WR")],
    )
    values = {
        "g": _iv("g", "Superstar Famous", 80),   # big name, low forward_value? no — high fv given away
        "x": _iv("x", "Anonymous Scrub", 30),
    }
    a = analyze_trade(state, values, "me", ["g"], ["x"])
    # I gave away the higher engine value → I lose, regardless of the names.
    # VOR delta (rescaled 0-100): give raw 16−8=8 → rescale 41.0, get 6<8→0 ⇒ -41.0.
    assert a.value_delta == -41.0
    assert a.winner == "opponent"


# ---------------------------------------------------------------------------
# Roster guard — the one locked rule
# ---------------------------------------------------------------------------
def test_roster_guard_fires_on_net_player_overflow_with_drop_rec():
    my = [RosterPlayer("a", "A", "WR"), RosterPlayer("b", "B", "WR"), RosterPlayer("c", "C", "WR")]
    opp = [RosterPlayer("x", "X", "WR"), RosterPlayer("y", "Y", "WR")]
    state = _two_team_state(my, opp)
    values = {
        "a": _iv("a", "A", 50), "b": _iv("b", "B", 10), "c": _iv("c", "C", 90),
        "x": _iv("x", "X", 40), "y": _iv("y", "Y", 30),
    }
    # 2-for-1 at a 3-slot limit → 4 players, over by 1.
    a = analyze_trade(state, values, "me", ["a"], ["x", "y"], roster_limit=3)
    g = a.roster_guard
    assert g.triggered is True
    assert g.net_players == 1
    # Lowest-value keeper (B, fv 10; A is being traded) is the drop rec.
    assert [r["name"] for r in g.drop_recommendations] == ["B"]


def test_roster_guard_silent_on_balanced_swap():
    my = [RosterPlayer("a", "A", "WR"), RosterPlayer("b", "B", "WR"), RosterPlayer("c", "C", "WR")]
    opp = [RosterPlayer("x", "X", "WR")]
    state = _two_team_state(my, opp)
    values = {"a": _iv("a", "A", 50), "b": _iv("b", "B", 10), "c": _iv("c", "C", 90), "x": _iv("x", "X", 40)}
    a = analyze_trade(state, values, "me", ["a"], ["x"], roster_limit=3)
    assert a.roster_guard.triggered is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_validate_rejects_unknown_team_and_misplaced_players():
    state = _two_team_state([RosterPlayer("g", "G", "WR")], [RosterPlayer("x", "X", "WR")])
    values = {"g": _iv("g", "G", 50), "x": _iv("x", "X", 50)}
    with pytest.raises(TradeValidationError):
        validate_trade(state, values, "nope", ["g"], ["x"])     # unknown team
    with pytest.raises(TradeValidationError):
        validate_trade(state, values, "me", ["x"], ["g"])       # give not on my team
    with pytest.raises(TradeValidationError):
        validate_trade(state, values, "me", ["g"], [])          # empty side


# ---------------------------------------------------------------------------
# Empty-slot WARNING (#179 follow-up) — a surfaced position-punt discloses the
# emptied required starter slot (a heads-up; the value math still credits the
# streamable replacement, so the trade is fine — the user just must be told).
# ---------------------------------------------------------------------------
def _full_roster_state():
    """Acquirer with a full 1QB/2RB/3WR/1TE/1K/1DST lineup + an opponent with a
    WR/TE/QB to acquire, so a trade can empty a required slot. K/DST fill their own
    slots (slice 3), so the baseline is genuinely full."""
    my = [RosterPlayer(p, p, pos) for p, pos in [
        ("qb", "QB"), ("rb1", "RB"), ("rb2", "RB"), ("rb3", "RB"),
        ("wr1", "WR"), ("wr2", "WR"), ("wr3", "WR"), ("te", "TE"),
        ("k", "K"), ("dst", "DEF")]]
    opp = [RosterPlayer(p, p, pos) for p, pos in [
        ("owr", "WR"), ("owr2", "WR"), ("ote", "TE"), ("oqb", "QB")]]
    state = _two_team_state(my, opp)
    specs = [("qb", "QB", 30), ("rb1", "RB", 40), ("rb2", "RB", 35), ("rb3", "RB", 20),
             ("wr1", "WR", 38), ("wr2", "WR", 34), ("wr3", "WR", 30), ("te", "TE", 25),
             ("k", "K", 20), ("dst", "DEF", 20),
             ("owr", "WR", 45), ("owr2", "WR", 44), ("ote", "TE", 22), ("oqb", "QB", 28)]
    values = {p: _iv(p, p, fv, pos=pos) for p, pos, fv in specs}
    return state, values


def test_emptying_only_te_fires_warning_with_position():
    state, values = _full_roster_state()
    a = analyze_trade(state, values, "me", ["te"], ["owr"], roster_limit=16)  # 0 TE after
    assert len(a.warnings) == 1
    w = a.warnings[0]
    assert w.type == "empty_required_slot" and w.position == "TE"
    assert "only TE" in w.message


def test_filled_slot_trade_has_no_warning():
    state, values = _full_roster_state()
    a = analyze_trade(state, values, "me", ["wr3"], ["owr"], roster_limit=16)  # keeps all slots
    assert a.warnings == ()


def test_emptying_two_required_slots_fires_two_warnings():
    state, values = _full_roster_state()
    a = analyze_trade(state, values, "me", ["qb", "te"], ["owr", "owr2"], roster_limit=16)
    positions = {w.position for w in a.warnings}
    assert positions == {"QB", "TE"}
    assert all(w.type == "empty_required_slot" for w in a.warnings)


def test_unfilled_flex_is_not_flagged():
    # A 7-player roster fills all REQUIRED slots (1QB/2RB/3WR/1TE) but leaves the
    # FLEX empty. A trade that keeps every required slot filled → NO warning
    # (FLEX is flexible; only fixed required slots count).
    my = [RosterPlayer(p, p, pos) for p, pos in [
        ("qb", "QB"), ("rb1", "RB"), ("rb2", "RB"),
        ("wr1", "WR"), ("wr2", "WR"), ("wr3", "WR"), ("te", "TE"),
        ("k", "K"), ("dst", "DEF")]]
    opp = [RosterPlayer("owr", "owr", "WR")]
    state = _two_team_state(my, opp)
    specs = [("qb", "QB", 30), ("rb1", "RB", 40), ("rb2", "RB", 35),
             ("wr1", "WR", 38), ("wr2", "WR", 34), ("wr3", "WR", 30), ("te", "TE", 25),
             ("k", "K", 20), ("dst", "DEF", 20),
             ("owr", "WR", 45)]
    values = {p: _iv(p, p, fv, pos=pos) for p, pos, fv in specs}
    a = analyze_trade(state, values, "me", ["wr3"], ["owr"], roster_limit=16)  # FLEX stays empty
    assert a.warnings == ()          # unfilled FLEX is NOT a warning


# ---------------------------------------------------------------------------
# League-local waiver-aware VOR (the K/DEF over-valuation fix)
# ---------------------------------------------------------------------------
from backend.services.trade.value_engine import (  # noqa: E402
    derive_anchors,
    rescale_vor,
    trade_value,
    vor_value,
    waiver_aware_replacement,
)


def test_deep_waiver_correction_lowers_wr_not_shallow_def():
    """WR/TE get the deep-waiver replacement correction: with a full WR pool the
    replacement drops to the MARGINAL ROSTER SPOT (below the starter-cutoff band the wire
    inflates), so a mid WR reads a real value instead of ~0. DEF is NOT deep-waiver
    corrected — (Build C) it gets the STREAMING baseline (mean of the top of its pool),
    which sits at/above the top of the shallow rostered pool, not the marginal spot."""
    vals = {}
    for i in range(50):                       # 50 rostered WRs — deep past the 42 starter line
        ppg = 20.0 - i * 0.36
        vals[f"w{i}"] = _iv(f"w{i}", f"WR{i}", ppg * 5, pos="WR")   # fv/5 == ppg
    for i in range(14):                       # shallow DEF pool (≈ the 12 starter line, no bench)
        vals[f"d{i}"] = _iv(f"d{i}", f"DEF{i}", (12.0 - i * 0.7) * 5, pos="DEF")
    wire = {"WR": [10.0] * 20}                 # a rich wire that inflates the starter-cutoff band
    repl = waiver_aware_replacement(vals, wire)

    pool_wr = [v.forward_ppg for v in vals.values() if v.position == "WR"] + wire["WR"]
    plain_wr = derive_anchors({"WR": pool_wr})["WR"][0]
    assert repl["WR"] < plain_wr               # WR corrected DOWN to the marginal roster spot
    # a mid WR (~10 ppg) now clears the floor instead of reading ~0
    mid = next(v for v in vals.values() if v.position == "WR" and 9.5 <= v.forward_ppg <= 10.5)
    assert trade_value(mid, repl) > 5.0
    # DEF (no wire here) → streaming baseline = mean of the top-3 of its rostered pool —
    # the TOP of the pool, far above the deep-waiver marginal spot the WRs got.
    assert repl["DEF"] == round((12.0 + 11.3 + 10.6) / 3, 1)
    assert repl["DEF"] > repl["WR"]


def test_rescale_vor_anchors_top_and_pins_floor():
    """The 0-100 display rescale: anchor the top to 100, PIN the floor at 0, and keep
    near-zero VOR near-zero — NOT a uniform stretch (which would lift a streamable DEF
    to ~22 and re-break the K/DEF fix)."""
    assert rescale_vor(0.0) == 0.0                      # floor pinned
    assert rescale_vor(-1.0) == 0.0                     # below replacement → 0
    assert rescale_vor(12.0) == 100.0                   # elite anchor → 100
    assert rescale_vor(20.0) == 100.0                   # above anchor clamps (no overflow)
    # an elite RB (~11.7 VOR) returns to the ~top of the scale
    assert rescale_vor(11.7) >= 88.0
    # a streamable DEF (~2.6 VOR) STAYS low — the anchor-the-top curve does NOT
    # multiply it up the way a naive linear stretch (2.6 * 100/12 ≈ 22) would.
    assert rescale_vor(2.6) <= 5.0
    # monotonic: more VOR → more display value
    assert rescale_vor(2.6) < rescale_vor(6.0) < rescale_vor(11.7)


def test_vor_value_floors_at_zero_and_runs_uncapped():
    repl = {"DEF": 6.0, "RB": 8.0}
    assert vor_value(9.0, "DEF", repl) == 3.0      # above replacement → the margin
    assert vor_value(5.0, "DEF", repl) == 0.0      # below replacement → floored to 0, never negative
    assert vor_value(30.0, "RB", repl) == 22.0     # NO cap — a genuine elite outlier runs free
    assert vor_value(9.0, "K", repl) == 9.0        # position absent from replacement → 0 floor


def test_waiver_wire_lowers_vor_by_raising_replacement():
    """The wire does the compression (derive_anchors mechanism, skill positions):
    adding streamable RBs to the pool RAISES the RB replacement, so the same RB's VOR
    DROPS (uncompressed without the wire). (Build C moved K/DEF onto the streaming
    baseline — see test_kdef_streaming_baseline — so this asserts on RB, where the
    derive_anchors band mechanism still governs.)"""
    # 40 rostered RBs so derive_anchors can derive (30-starter demand + a full
    # below-cutoff band), then a wire of streamable RBs.
    vals = {}
    for i in range(40):
        pid = f"r{i}"
        vals[pid] = _iv(pid, f"RB{i}", 50, pos="RB")
        vals[pid].forward_ppg = 18.0 - i * 0.3        # 18.0 down to ~6.3
    wire = {"RB": [7.5, 7.2, 6.9, 6.6, 6.3]}
    repl_no = waiver_aware_replacement(vals, {})["RB"]
    repl_wire = waiver_aware_replacement(vals, wire)["RB"]
    assert repl_wire >= repl_no                       # wire adds below-cutoff streamers
    # a 9.5-ppg RB is worth LESS over the waiver-aware replacement
    assert vor_value(9.5, "RB", {"RB": repl_wire}) <= vor_value(9.5, "RB", {"RB": repl_no})


def test_kdef_streaming_baseline_collapses_def_value():
    """(Build C) K/DEF replacement = the STREAMING baseline (mean of the top wire
    options), NOT the derive_anchors band or the _PPG_ANCHORS sparse-pool fallback —
    a 12-team league rosters exactly 12 DEF (the cutoff, zero bench), so the derived
    path could never work and the fallback (5.0) inflated DEF VOR (an elite DEF read
    like an elite WR). With the wire's best streamers at ~11-12 ppg, even a 13.3-ppg
    top DEF keeps only a small margin, and a startable 9.9-ppg DEF floors at 0."""
    vals = {}
    for i in range(12):                               # exactly 12 rostered DEF = cutoff
        pid = f"d{i}"
        vals[pid] = _iv(pid, f"DEF{i}", 50, pos="DEF")
        vals[pid].forward_ppg = 13.3 - i * 1.0
    wire = {"DEF": [11.8, 11.7, 9.9, 8.1, 7.9]}
    repl = waiver_aware_replacement(vals, wire)
    assert repl["DEF"] == round((11.8 + 11.7 + 9.9) / 3, 1)   # mean of top-3 wire
    top = max(vals.values(), key=lambda v: v.forward_ppg)
    assert 0.0 < trade_value(top, repl) <= 5.0        # top DEF: small margin, NOT the 30s
    mid = next(v for v in vals.values() if 9.0 <= v.forward_ppg <= 10.5)
    assert trade_value(mid, repl) == 0.0              # startable-but-not-elite DEF → 0
    # thin wire → falls back to rostered ∪ wire, ~the same level (pool-independent)
    repl_thin = waiver_aware_replacement(vals, {})
    assert repl_thin["DEF"] == round((13.3 + 12.3 + 11.3) / 3, 1)  # top-3 of rostered
    assert trade_value(top, repl_thin) <= 5.0
