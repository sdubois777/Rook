"""
Tests for contextual_value (backend/services/trade/contextual.py) — slice 2 of
the trade acceptability model. The headline is the TWO-ROSTER ASYMMETRY: the
same player is worth little to a roster deep at his position and a lot to a thin
one — what makes positive-sum trades representable.
"""
from __future__ import annotations

import backend.services.trade.contextual as ctx
from backend.services.trade.contextual import contextual_value
from backend.services.trade.lineup import LineupPlayer


def _p(pid, pos, fv):
    return LineupPlayer(player_id=pid, position=pos, forward_value=fv)


def _qbwrte_filler():
    """A QB / 3 WR / TE block so the lineup around the RBs is realistic."""
    return [_p("q", "QB", 22), _p("w1", "WR", 19), _p("w2", "WR", 17),
            _p("w3", "WR", 15), _p("w4", "WR", 13), _p("t", "TE", 11)]


# ---------------------------------------------------------------------------
# THE headline test — two-roster asymmetry
# ---------------------------------------------------------------------------
def test_same_player_low_to_rich_roster_high_to_thin_roster():
    player = _p("P", "RB", 15)                      # a startable-ish RB

    rich = [*_qbwrte_filler(),                       # 4 startable RBs already
            _p("r1", "RB", 20), _p("r2", "RB", 18),
            _p("r3", "RB", 16), _p("r4", "RB", 14)]
    thin = [*_qbwrte_filler(), _p("r1", "RB", 6)]    # one weak RB

    v_rich = contextual_value(player, rich)
    v_thin = contextual_value(player, thin)

    assert v_thin == 15.0          # he becomes a real starter — full upgrade
    assert v_rich < 8.0            # redundant depth — near the bench floor
    assert v_thin > v_rich + 5     # clear, decisive asymmetry (the whole point)


# ---------------------------------------------------------------------------
# Starter upgrade == displacement (not full forward_value)
# ---------------------------------------------------------------------------
def test_starter_upgrade_is_value_minus_displaced_weakest_starter():
    # Full 8-slot roster: QB / 2 RB / 3 WR / TE / FLEX all filled.
    roster = [_p("q", "QB", 20), _p("r1", "RB", 10), _p("r2", "RB", 4),
              _p("w1", "WR", 18), _p("w2", "WR", 16), _p("w3", "WR", 14),
              _p("w4", "WR", 12), _p("t", "TE", 8)]
    player = _p("P", "RB", 13)     # cracks RB1, displaces the weakest RB starter (4)

    v = contextual_value(player, roster)
    assert v == 13 - 4             # his value minus the displaced starter, NOT 13
    assert v == 9.0


# ---------------------------------------------------------------------------
# Clear starter vs genuine sit
# ---------------------------------------------------------------------------
def test_elite_player_on_weak_roster_reads_high():
    roster = [_p("q", "QB", 10), _p("r1", "RB", 3),
              _p("w1", "WR", 5), _p("w2", "WR", 4), _p("w3", "WR", 3), _p("t", "TE", 2)]
    v = contextual_value(_p("P", "RB", 25), roster)
    assert v > 20                  # massive upgrade on a weak roster


def test_genuine_sit_reads_low_but_not_zero_and_below_full_value():
    deep_rb = [*_qbwrte_filler(),
               _p("r1", "RB", 20), _p("r2", "RB", 18),
               _p("r3", "RB", 16), _p("r4", "RB", 14)]
    v = contextual_value(_p("P", "RB", 8), deep_rb)   # mediocre RB, roster deep at RB
    assert 0 < v < 3               # near the bench-depth floor — not 0, well below 8


# ---------------------------------------------------------------------------
# The steepness lever works
# ---------------------------------------------------------------------------
def test_lowering_steepness_constant_raises_bench_depth_value(monkeypatch):
    deep_rb = [*_qbwrte_filler(),
               _p("r1", "RB", 20), _p("r2", "RB", 18),
               _p("r3", "RB", 16), _p("r4", "RB", 14)]
    sit = _p("P", "RB", 8)
    steep = contextual_value(sit, deep_rb)             # default steepness 2.0
    monkeypatch.setattr(ctx, "_BENCH_DEPTH_STEEPNESS", 1.0)
    shallow = contextual_value(sit, deep_rb)
    assert shallow > steep         # less steep → a sit retains more value


# ---------------------------------------------------------------------------
# Degenerate
# ---------------------------------------------------------------------------
def test_player_on_empty_roster_is_worth_his_own_contribution():
    v = contextual_value(_p("P", "RB", 14), [])
    assert v == 14.0               # he just starts; no crash, no NaN


def test_player_on_thin_roster_starts_and_reads_full():
    v = contextual_value(_p("P", "WR", 12), [_p("q", "QB", 20)])
    assert v == 12.0               # open WR slot → full contribution
