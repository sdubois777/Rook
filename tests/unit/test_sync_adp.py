"""Unit tests for scripts/sync_adp.py — name normalization + ADP matching.

The DB/scrape boundary (sync_adp) is integration-tested elsewhere; here we test
the pure pieces: normalize_name (frontend mirror) and apply_adp (matching).
"""
from __future__ import annotations

from types import SimpleNamespace

from scripts.sync_adp import normalize_name, apply_adp
from backend.agents.valuation_agent import compute_adp_diff


def _p(name: str, position: str = "WR"):
    return SimpleNamespace(
        name=name, position=position, adp_fantasypros=None, adp_scoring=None
    )


class TestNormalizeName:
    def test_strips_generational_suffix(self):
        assert normalize_name("Travis Etienne Jr.") == "travis etienne"
        assert normalize_name("Kenneth Walker III") == "kenneth walker"

    def test_hyphen_maps_to_space(self):
        # The canonical Amon-Ra case: hyphenated == spaced.
        assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"
        assert normalize_name("Amon-Ra St. Brown") == normalize_name("Amon Ra St. Brown")

    def test_drops_punctuation_and_lowercases(self):
        assert normalize_name("Ja'Marr Chase") == "jamarr chase"
        assert normalize_name("A.J. Brown") == "aj brown"

    def test_safe_on_none(self):
        assert normalize_name(None) == ""


def test_sync_adp_matches_players():
    players = [_p("Amon-Ra St. Brown", "WR"), _p("Bijan Robinson", "RB")]
    adp_data = [
        # rank is what gets stored; adp (avg) is carried but ignored.
        {"name": "Amon Ra St. Brown", "position": "WR", "rank": 7, "adp": 8.5},  # punctuation diff
        {"name": "Bijan Robinson", "position": "RB", "rank": 2, "adp": 3.0},
        {"name": "Nobody Here", "position": "TE", "rank": 99, "adp": 99.0},       # no match
    ]

    summary = apply_adp(adp_data, players, "ppr")

    assert summary == {"matched": 2, "missed": 1, "scoring": "ppr", "total": 3}
    assert players[0].adp_fantasypros == 7  # rank, matched despite spelling difference
    assert players[1].adp_fantasypros == 2


def test_sync_adp_stores_rank_not_avg():
    # The whole point of this change: when a row has both rank and avg, store the
    # RANK (same scale as adp_ai), never the avg.
    players = [_p("Bijan Robinson", "RB")]
    apply_adp(
        [{"name": "Bijan Robinson", "position": "RB", "rank": 2, "adp": 1.5}], players, "ppr"
    )
    assert players[0].adp_fantasypros == 2.0   # the rank
    assert players[0].adp_fantasypros != 1.5   # not the avg


def test_adp_diff_uses_rank_scale():
    # With both sides on the overall-rank scale, diff is a clean pick difference:
    # positive = FP ranks them later than us (we like them more).
    assert compute_adp_diff(50, 32) == 18.0   # Lamar: fp_rank 50, ai 32 -> +18
    assert compute_adp_diff(2, 4) == -2.0      # Bijan: fp_rank 2, ai 4 -> -2


def test_sync_adp_normalize_name():
    # Suffix-only difference must still match.
    players = [_p("Michael Pittman", "WR")]
    summary = apply_adp(
        [{"name": "Michael Pittman Jr.", "position": "WR", "rank": 40, "adp": 40.0}], players, "ppr"
    )
    assert summary["matched"] == 1
    assert players[0].adp_fantasypros == 40


def test_sync_adp_scoring_format_stored():
    players = [_p("Bijan Robinson", "RB")]
    apply_adp(
        [{"name": "Bijan Robinson", "position": "RB", "rank": 2, "adp": 3.0}], players, "half_ppr"
    )
    assert players[0].adp_scoring == "half_ppr"


def test_sync_adp_ambiguous_name_disambiguated_by_position():
    # Two distinct players share a normalized name — position breaks the tie.
    rb = _p("Jonathan Taylor", "RB")
    wr = _p("Jonathan Taylor", "WR")
    apply_adp(
        [{"name": "Jonathan Taylor", "position": "RB", "rank": 8, "adp": 10.0}], [rb, wr], "ppr"
    )
    assert rb.adp_fantasypros == 8
    assert wr.adp_fantasypros is None  # not wrongly assigned


def test_sync_adp_skips_rows_with_no_rank():
    players = [_p("Bijan Robinson", "RB")]
    summary = apply_adp(
        [{"name": "Bijan Robinson", "position": "RB", "rank": None, "adp": 3.0}], players, "ppr"
    )
    assert summary["matched"] == 0
    assert players[0].adp_fantasypros is None
