"""One malformed model row must not abort the pipeline.

`_run_tier_batches` indexed model output as `r["player_name"]`. A single row missing that
key raised KeyError, which propagated out of `run_prose_for_format` and killed the WHOLE
pipeline run — taking `format_market`, `team_notes` and the availability discount with it,
because those phases come after. Observed on 3 of 3 runs, production included.

The board is expensive; the prose is not. Losing three stages to one unusable row is the
wrong trade.
"""
from __future__ import annotations

import logging

from backend.agents.valuation_agent import _stash


def test_a_row_missing_player_name_is_skipped_not_raised():
    """THE regression. This exact shape aborted a production pipeline run."""
    results: dict = {}
    _stash(results, {"ai_bid_ceiling": 40, "auction_note": "no name on this row"})
    assert results == {}


def test_a_good_row_is_recorded():
    results: dict = {}
    row = {"player_name": "Ja'Marr Chase", "ai_bid_ceiling": 39}
    _stash(results, row)
    assert results == {"Ja'Marr Chase": row}


def test_one_bad_row_does_not_lose_the_good_ones():
    """The batch is what matters — a single unusable entry must not discard its siblings."""
    results: dict = {}
    for row in (
        {"player_name": "Good One", "ai_bid_ceiling": 10},
        {"ai_bid_ceiling": 20},                    # no name
        {"player_name": "", "ai_bid_ceiling": 30},  # empty name
        {"player_name": "Good Two", "ai_bid_ceiling": 40},
    ):
        _stash(results, row)
    assert sorted(results) == ["Good Two", "Good One"][::-1] or sorted(results) == ["Good One", "Good Two"]
    assert len(results) == 2


def test_empty_name_is_treated_as_missing():
    results: dict = {}
    _stash(results, {"player_name": "", "x": 1})
    _stash(results, {"player_name": None, "x": 1})
    assert results == {}


def test_non_dict_rows_are_survivable():
    """The model has returned bare strings and lists before; neither should raise."""
    results: dict = {}
    for junk in ("just a string", ["a", "list"], 42, None):
        _stash(results, junk)
    assert results == {}


def test_it_warns_rather_than_failing_silently(caplog):
    """A skipped player is a real gap in the board — it must be visible in the log, or a
    run quietly ships fewer valuations than it reports."""
    results: dict = {}
    with caplog.at_level(logging.WARNING):
        _stash(results, {"ai_bid_ceiling": 5})
    assert any("player_name" in r.message or "player_name" in str(r.args)
               for r in caplog.records), caplog.text


def test_later_rows_win_for_the_same_player():
    """Dict semantics preserved — the tier batches rely on last-write-wins."""
    results: dict = {}
    _stash(results, {"player_name": "Dup", "v": 1})
    _stash(results, {"player_name": "Dup", "v": 2})
    assert results["Dup"]["v"] == 2
