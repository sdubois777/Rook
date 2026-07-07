"""FAAB bid heuristic — tiers, floor, cap, news stash, scarcity (pure)."""
from __future__ import annotations

from backend.services.waiver.faab import FAAB_MIN_BID, suggest_bid


def _bid(gain, remaining=100, **kw):
    return suggest_bid(gain_ppw=gain, faab_remaining=remaining, **kw)


def test_tier_mapping_by_gain():
    assert _bid(8.0).tier_label == "league-winner"
    assert _bid(3.0).tier_label == "week-winning starter"
    assert _bid(1.5).tier_label == "flex / matchup play"
    assert _bid(0.5).tier_label == "speculative stash"


def test_token_floor_for_recommended():
    b = _bid(0.5, remaining=100)   # 2% of 100 = $2, but even a tiny % floors at $1
    assert b.recommended and b.total_bid >= FAAB_MIN_BID


def test_never_exceeds_remaining():
    b = _bid(8.0, remaining=3)      # 40%+ of 3 would round high; capped at 3
    assert b.total_bid <= 3


def test_not_recommended_without_gain_or_news():
    b = _bid(0.0)
    assert b.recommended is False and b.total_bid == 0


def test_speculative_stash_on_fresh_signal_with_zero_gain():
    b = _bid(0.0, has_news_bump=True)
    assert b.recommended is True and b.tier_label == "speculative stash"
    assert b.total_bid >= FAAB_MIN_BID


def test_news_bump_is_separate_and_additive():
    base = _bid(3.0, remaining=100)
    bumped = _bid(3.0, remaining=100, has_news_bump=True)
    assert bumped.news_bump_bid > 0
    assert bumped.total_bid == base.base_bid + bumped.news_bump_bid


def test_scarcity_raises_bid_within_tier():
    plain = _bid(3.0, remaining=100, value_over_replacement=0.0, replacement_ppg=8.0)
    scarce = _bid(3.0, remaining=100, value_over_replacement=8.0, replacement_ppg=8.0)
    assert scarce.total_bid > plain.total_bid


def test_no_budget_never_recommends():
    b = _bid(8.0, remaining=0, has_news_bump=True)
    assert b.recommended is False and b.total_bid == 0
