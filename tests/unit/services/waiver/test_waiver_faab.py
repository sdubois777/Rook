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


# ---------------------------------------------------------------------------
# Leagues that do NOT bid (waiver priority / reverse standings) pass remaining=None.
#
# Everything except the money still applies to them. Getting `recommended` wrong here
# is invisible in the dollar fields but ruins the whole page: the waiver page renders
# a not-recommended card as the flat text "not worth a claim" and discards the tier,
# so every add in a rolling-priority league — including a league-winner — would read
# as not worth claiming.
# ---------------------------------------------------------------------------
def _claim(gain, **kw):
    return suggest_bid(gain_ppw=gain, faab_remaining=None, **kw)


def test_priority_league_keeps_the_tier_and_still_recommends():
    b = _claim(8.0)
    assert b.recommended is True            # the card must render as a real target
    assert b.tier_label == "league-winner"  # ...with its tier intact
    assert b.bid_applicable is False


def test_priority_league_tiers_match_the_bidding_tiers_exactly():
    """Same gain, same tier label, with or without a budget. A league that does not
    bid gets the identical ranking advice — only the dollar amount is withheld."""
    for gain in (8.0, 3.0, 1.5, 0.5):
        assert _claim(gain).tier_label == _bid(gain).tier_label


def test_priority_league_produces_no_money_at_all():
    b = _claim(8.0, has_news_bump=True)
    assert (b.total_bid, b.base_bid, b.news_bump_bid) == (0, 0, 0)
    assert b.base_pct == 0.0 and b.pct_of_remaining == 0.0
    # The explanation must not name a sum or a share of one.
    assert "$" not in b.why and "%" not in b.why


def test_priority_league_still_declines_a_worthless_add():
    b = _claim(0.0)
    assert b.recommended is False
    assert b.tier_label == "not worth a claim"
    assert b.bid_applicable is False


def test_priority_league_still_stashes_on_a_fresh_signal():
    b = _claim(0.0, has_news_bump=True)
    assert b.recommended is True
    assert b.tier_label == "speculative stash"
    assert b.total_bid == 0 and b.bid_applicable is False


def test_bidding_leagues_are_untouched_by_the_no_bid_path():
    """A real budget still marks the suggestion as biddable — the flag defaults to
    True, so an omission would silently strip the money from every league."""
    assert _bid(8.0).bid_applicable is True
    assert _bid(8.0, remaining=0).bid_applicable is True   # spent out, but still bids
