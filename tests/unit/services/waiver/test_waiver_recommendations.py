"""
Waiver recommendation engine (pure): the add/drop lineup-gain objective composed
from the trade primitives, best-drop selection, position-need boost, and the
news-stash inclusion. No DB, no LLM.
"""
from __future__ import annotations

import pytest

from backend.services.trade.league_state import RosterPlayer, TeamState
from backend.services.trade.lineup import LineupRules
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend
from backend.services.waiver.news_tiein import NewsInfo
from backend.services.waiver.recommendations import NEED_RANK_BONUS, best_add, recommend

# 1-WR lineup keeps hand-computed gains clean (no empty-slot replacement noise).
RULES_1WR = LineupRules(slots={"WR": 1}, flex_count=0, flex_positions=())
RULES_QB_WR = LineupRules(slots={"QB": 1, "WR": 1}, flex_count=0, flex_positions=())


def _iv(pid, fv, ppg, *, pos="WR"):
    return InSeasonValue(
        canonical_player_id=pid, name=pid.upper(), position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=8, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=ppg, expected_ppg=ppg, opportunity_gap=0.0, sustainable=True,
        forward_ppg=ppg, schedule_modifier=0.0, prior_projection=None, prior_weight=0.0,
        name_bias_guard_applied=False, confidence=Confidence.FULL, confidence_reason="",
    )


def _team(*players):
    return TeamState("me", "Me", True, tuple(players))


def test_add_improves_lineup_open_slot_under_limit():
    me = _team(RosterPlayer("a", "A", "WR"))
    pool = [RosterPlayer("b", "B", "WR")]
    values = {"a": _iv("a", 50, 10), "b": _iv("b", 90, 15)}
    recs = recommend(me, pool, values, rules=RULES_1WR, roster_limit=16, faab_remaining=100)
    assert len(recs) == 1
    assert recs[0].add.canonical_player_id == "b"
    assert recs[0].lineup_delta_ppw == 5.0     # 15 (b starts) − 10 (a)
    assert recs[0].drop is None                # roster below limit → open slot


def test_best_drop_at_limit_is_the_marginal_player():
    # roster at the limit: adding a WR forces a drop; the marginal bench RB (lowest
    # forward_value) is dropped, never the starting WR.
    me = _team(RosterPlayer("a", "A", "WR"), RosterPlayer("z", "Z", "RB"))
    pool = [RosterPlayer("b", "B", "WR")]
    values = {"a": _iv("a", 50, 10), "z": _iv("z", 10, 3, pos="RB"), "b": _iv("b", 90, 15)}
    recs = recommend(me, pool, values, rules=RULES_1WR, roster_limit=2, faab_remaining=100)
    assert recs[0].drop is not None
    assert recs[0].drop.id == "z"              # the marginal player, not starter "a"
    assert recs[0].lineup_delta_ppw == 5.0


def test_zero_gain_pool_player_included_only_with_news():
    me = _team(RosterPlayer("a", "A", "WR"))
    pool = [RosterPlayer("c", "C", "WR")]
    values = {"a": _iv("a", 90, 20), "c": _iv("c", 10, 2)}
    # No news → the sub-replacement add is not surfaced at all.
    assert recommend(me, pool, values, rules=RULES_1WR, roster_limit=16, faab_remaining=100) == []
    # With a fresh signal → surfaced as a speculative stash.
    news = {"c": NewsInfo(kind="direct", headline="C is a camp standout", signal_type="camp_standout",
                          confidence="low", source="x", flagged_at=None)}
    recs = recommend(me, pool, values, rules=RULES_1WR, roster_limit=16, faab_remaining=100, news_map=news)
    assert len(recs) == 1 and recs[0].news is not None
    assert recs[0].faab.tier_label == "speculative stash" and recs[0].faab.total_bid >= 1


def test_position_need_applies_ranking_bonus():
    # Roster weak at QB (a low-value QB starter below the weak-starter threshold) →
    # QB is a need; a QB add gets the ranking bonus.
    me = _team(RosterPlayer("q", "Q", "QB"), RosterPlayer("w", "W", "WR"))
    pool = [RosterPlayer("q2", "Q2", "QB"), RosterPlayer("w2", "W2", "WR")]
    values = {
        "q": _iv("q", 10, 5, pos="QB"), "w": _iv("w", 80, 16),
        "q2": _iv("q2", 60, 12, pos="QB"), "w2": _iv("w2", 60, 12),
    }
    recs = recommend(me, pool, values, rules=RULES_QB_WR, roster_limit=16, faab_remaining=100)
    top = recs[0]
    assert top.add.canonical_player_id == "q2" and top.fills_need is True
    # White-box: the rank score is the honest gain plus the need bonus (no news here).
    assert top._rank_score == pytest.approx(top.lineup_delta_ppw + NEED_RANK_BONUS)


def test_best_add_near_miss_returns_top_even_when_nothing_qualifies():
    me = _team(RosterPlayer("a", "A", "WR"))
    pool = [RosterPlayer("c", "C", "WR")]
    values = {"a": _iv("a", 90, 20), "c": _iv("c", 10, 2)}
    assert recommend(me, pool, values, rules=RULES_1WR, roster_limit=16, faab_remaining=100) == []
    nm = best_add(me, pool, values, rules=RULES_1WR, roster_limit=16)
    assert nm is not None and nm[0].canonical_player_id == "c" and nm[1] == 0.0
