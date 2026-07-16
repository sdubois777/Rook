"""Phase 2 (part 1) — the TRADE VERDICT is format-aware via LIVE re-scoring.

The in-season engine re-derives value from live per-week production, so format-awareness
re-scores the live per-category points (ppr/half/standard) — it does NOT read the pre-draft
player_format_values table. These tests prove:
  * PPR re-scoring is the IDENTITY (byte-identical to today) — the hard guarantee.
  * a reception-dependent player's forward_ppg DROPS in Standard.
  * the VERDICT (lineup_gain / winner) MOVES — and here flips — by format.
"""
from __future__ import annotations

import pandas as pd

from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_analysis import analyze_trade, validate_trade
from backend.services.trade.value_engine import evaluate_league


def _weeks(cid, snaps, ppr, std, *, targets, carries):
    """Per-player weekly frame carrying BOTH the ppr and std points columns (the #149
    layer emits both; std = ppr − receptions). half = (ppr+std)/2 is derived by the engine."""
    n = len(ppr)
    return pd.DataFrame({
        "canonical_player_id": [cid] * n,
        "week": list(range(1, n + 1)),
        "snap_pct": snaps,
        "target_share": [targets] * n,
        "fantasy_points_ppr": ppr,
        "fantasy_points_std": std,
        "targets": [targets * 10] * n,
        "carries": [carries] * n,
    })


# A reception MONSTER (10 rec/wk → ppr 20, std 10) vs a balanced WR (2 rec/wk → ppr 16,
# std 14). Both WR, so a give-balanced / get-catchy swap is a clean same-slot Δ.
def _weekly():
    return pd.concat([
        _weeks("catchy",   [0.9] * 5, [20.0] * 5, [10.0] * 5, targets=0.30, carries=0),
        _weeks("balanced", [0.85] * 5, [16.0] * 5, [14.0] * 5, targets=0.18, carries=0),
        _weeks("qb1",      [1.0] * 5, [18.0] * 5, [18.0] * 5, targets=0.0, carries=0),
        _weeks("rb1",      [0.8] * 5, [15.0] * 5, [15.0] * 5, targets=0.05, carries=15),
    ], ignore_index=True)


def _state():
    # My team holds the balanced WR (a starter); their team holds the reception monster.
    return LeagueState(
        season=2025, week=6,
        teams=(
            TeamState("me", "Me", is_me=True, roster=(
                RosterPlayer("balanced", "Balanced WR", "WR"),
                RosterPlayer("qb1", "QB One", "QB"),
                RosterPlayer("rb1", "RB One", "RB"),
            )),
            TeamState("them", "Them", is_me=False, roster=(
                RosterPlayer("catchy", "Catchy WR", "WR"),
            )),
        ),
    )


def _verdict(fmt):
    state = _weekly(), _state()
    weekly, st = state
    values = evaluate_league(st, weekly, scoring_format=fmt, priors={})
    validate_trade(st, values, "me", ["balanced"], ["catchy"])
    return analyze_trade(st, values, "me", ["balanced"], ["catchy"])


def test_ppr_rescore_is_identity():
    """PPR MUST be byte-identical: evaluate_league with no format == scoring_format='ppr'."""
    weekly, st = _weekly(), _state()
    default = evaluate_league(st, weekly, priors={})
    explicit_ppr = evaluate_league(st, weekly, scoring_format="ppr", priors={})
    for pid in default:
        assert default[pid].forward_ppg == explicit_ppr[pid].forward_ppg
        assert default[pid].forward_value == explicit_ppr[pid].forward_value


def test_reception_player_ppg_drops_in_standard():
    weekly, st = _weekly(), _state()
    ppr = evaluate_league(st, weekly, scoring_format="ppr", priors={})
    std = evaluate_league(st, weekly, scoring_format="standard", priors={})
    # The reception monster loses ~10 ppg of receptions in Standard.
    assert std["catchy"].forward_ppg < ppr["catchy"].forward_ppg
    # The rush/TD RB (0 receptions) is format-invariant.
    assert std["rb1"].forward_ppg == ppr["rb1"].forward_ppg


def test_trade_verdict_flips_by_format():
    """The VERDICT itself moves: getting the reception monster for the balanced WR is a
    WIN in PPR and a LOSS in Standard (lineup_gain changes sign)."""
    ppr = _verdict("ppr")
    half = _verdict("half_ppr")
    std = _verdict("standard")
    assert ppr.lineup_gain > 0 and ppr.winner == "you"      # PPR: getting Catchy improves the lineup
    assert std.lineup_gain < 0 and std.winner == "opponent"  # Standard: it's a downgrade
    assert half.lineup_gain < ppr.lineup_gain                # Half sits between
    # PPR is unchanged vs the pre-format default path.
    assert _verdict("ppr").lineup_gain == analyze_trade(
        _state(), evaluate_league(_state(), _weekly(), priors={}), "me", ["balanced"], ["catchy"],
    ).lineup_gain
