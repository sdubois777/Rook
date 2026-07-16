"""Tests for the auction-value URL builder — the G5 FLEX FIX and PPR isolation.

The default DraftWizard URL omits the flex slot; passing the canonical roster appends
roster-slot params (incl. flex=1). The DEFAULT (roster=None) URL must be byte-identical
to the pre-G5 form so the players-table market_value PPR path is unchanged.
"""
from __future__ import annotations

from backend.integrations.fantasypros import _roster_query
from backend.services.format_market_ingest import CANONICAL_ROSTER


def test_roster_query_includes_flex_and_all_slots():
    q = _roster_query(CANONICAL_ROSTER)
    # Canonical shape: QB1/RB2/WR3/TE1/FLEX1/DST1/K1/BN6 in URL param form.
    assert q == "&qb=1&rb=2&wr=3&te=1&flex=1&dst=1&k=1&bench=6"
    assert "&flex=1" in q  # THE fix — default URLs omit this


def test_roster_query_empty_when_no_roster():
    assert _roster_query({}) == ""


def test_roster_query_only_emits_provided_slots():
    assert _roster_query({"RB": 2, "FLEX": 1}) == "&rb=2&flex=1"
