"""Unit tests for the per-format ADP + auction ingest (G5).

Covers the PURE matcher (build_format_market_upserts) and the RE-SCRAPE GATE — the
whole point of G5 — that a second ingest run against CHANGED source data serves the new
values, not a cached snapshot. Scrapers + persistence are injected so no browser/DB is
touched.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from backend.services.format_market_ingest import (
    CANONICAL_ROSTER,
    CANONICAL_ROSTER_SHAPE,
    build_format_market_upserts,
    ingest_format_market_data,
)


def _p(name: str, position: str = "WR"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, position=position)


def _adp(name, position, rank):
    return {"name": name, "position": position, "rank": rank}


def _auc(name, position, value):
    return {"name": name, "position": position, "avg_value": value}


# --------------------------------------------------------------------------- pure matcher
class TestBuildUpserts:
    def test_matches_adp_and_auction_into_one_row(self):
        players = [_p("Bijan Robinson", "RB")]
        rows, summary = build_format_market_upserts(
            [_adp("Bijan Robinson", "RB", 2)],
            [_auc("Bijan Robinson", "RB", 55.0)],
            players, "ppr", CANONICAL_ROSTER_SHAPE,
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["player_id"] == players[0].id
        assert r["scoring_format"] == "ppr"
        assert r["adp_fantasypros"] == 2.0
        assert r["auction_value"] == 55.0
        assert r["auction_roster_shape"] == CANONICAL_ROSTER_SHAPE
        assert summary["adp_matched"] == 1 and summary["auction_matched"] == 1

    def test_normalized_name_match_survives_punctuation_and_suffix(self):
        players = [_p("Amon-Ra St. Brown", "WR"), _p("Michael Pittman", "WR")]
        rows, summary = build_format_market_upserts(
            [_adp("Amon Ra St. Brown", "WR", 7), _adp("Michael Pittman Jr.", "WR", 40)],
            [], players, "ppr", CANONICAL_ROSTER_SHAPE,
        )
        by_id = {r["player_id"]: r for r in rows}
        assert by_id[players[0].id]["adp_fantasypros"] == 7.0
        assert by_id[players[1].id]["adp_fantasypros"] == 40.0
        assert summary["adp_matched"] == 2

    def test_ambiguous_name_disambiguated_by_position(self):
        rb = _p("Jonathan Taylor", "RB")
        wr = _p("Jonathan Taylor", "WR")
        rows, _ = build_format_market_upserts(
            [_adp("Jonathan Taylor", "RB", 8)], [], [rb, wr], "ppr", CANONICAL_ROSTER_SHAPE,
        )
        assert len(rows) == 1 and rows[0]["player_id"] == rb.id

    def test_unmatched_rows_are_counted_not_dropped(self):
        players = [_p("Bijan Robinson", "RB")]
        rows, summary = build_format_market_upserts(
            [_adp("Nobody Here", "TE", 99)],
            [_auc("Ghost Player", "WR", 12.0)],
            players, "ppr", CANONICAL_ROSTER_SHAPE,
        )
        assert rows == []
        assert summary["adp_unmatched"] == 1
        assert summary["auction_unmatched"] == 1
        assert "Nobody Here" in summary["adp_unmatched_names"]
        assert "Ghost Player" in summary["auction_unmatched_names"]

    def test_row_created_from_one_feed_only(self):
        # ADP-only match still produces a row (auction stays None, no roster shape).
        players = [_p("Kyle Pitts", "TE")]
        rows, _ = build_format_market_upserts(
            [_adp("Kyle Pitts", "TE", 60)], [], players, "half_ppr", CANONICAL_ROSTER_SHAPE,
        )
        assert len(rows) == 1
        assert rows[0]["adp_fantasypros"] == 60.0
        assert rows[0]["auction_value"] is None
        assert rows[0]["auction_roster_shape"] is None  # disclosure only when $ present

    def test_skips_rows_missing_rank_or_value(self):
        players = [_p("Bijan Robinson", "RB")]
        rows, summary = build_format_market_upserts(
            [_adp("Bijan Robinson", "RB", None)],
            [_auc("Bijan Robinson", "RB", None)],
            players, "ppr", CANONICAL_ROSTER_SHAPE,
        )
        assert rows == []
        assert summary["adp_total"] == 0 and summary["auction_total"] == 0

    def test_dst_alias_normalization_matches(self):
        players = [_p("San Francisco", "DST")]
        rows, _ = build_format_market_upserts(
            [], [_auc("San Francisco", "DEF", 3.0)], players, "ppr", CANONICAL_ROSTER_SHAPE,
        )
        assert len(rows) == 1 and rows[0]["auction_value"] == 3.0


# --------------------------------------------------------------- reception-dependent skew
class TestPerFormatDivergence:
    def test_pass_catcher_ranks_earlier_in_ppr_than_standard(self):
        """A reception-dependent player must carry a DIFFERENT ADP/auction per format —
        this is the observable proof the three feeds are ingested independently."""
        wr = _p("Puka Nacua", "WR")
        rows_ppr, _ = build_format_market_upserts(
            [_adp("Puka Nacua", "WR", 9)], [_auc("Puka Nacua", "WR", 48.0)],
            [wr], "ppr", CANONICAL_ROSTER_SHAPE,
        )
        rows_std, _ = build_format_market_upserts(
            [_adp("Puka Nacua", "WR", 18)], [_auc("Puka Nacua", "WR", 34.0)],
            [wr], "standard", CANONICAL_ROSTER_SHAPE,
        )
        assert rows_ppr[0]["adp_fantasypros"] < rows_std[0]["adp_fantasypros"]  # earlier in PPR
        assert rows_ppr[0]["auction_value"] > rows_std[0]["auction_value"]      # pricier in PPR


# ------------------------------------------------------------------------- RE-SCRAPE GATE
class _FakeResult:
    def __init__(self, players):
        self._players = players

    def scalars(self):
        return self

    def all(self):
        return self._players


class _FakeSession:
    """Minimal AsyncSession stand-in: serves the player list and no-ops commit."""

    def __init__(self, players):
        self._players = players
        self.commits = 0

    async def execute(self, _stmt):
        return _FakeResult(self._players)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_rescrape_gate_second_run_serves_new_values():
    """THE GATE: run ingest twice with source data that MOVED between runs; the second
    run must persist the NEW values (live re-scrape, not a cached snapshot)."""
    player = _p("Bijan Robinson", "RB")
    session = _FakeSession([player])

    # Source values MOVE between run 1 and run 2 (injury/camp/ADP drift simulation).
    scrape_calls = {"adp": 0, "auction": 0}

    def _adp_for_run(run: int):
        rank = 2 if run == 1 else 5  # drifted later
        return [_adp("Bijan Robinson", "RB", rank)]

    def _auc_for_run(run: int):
        value = 55.0 if run == 1 else 40.0  # dropped $
        return [_auc("Bijan Robinson", "RB", value)]

    run = {"n": 1}

    async def adp_scraper(*, scoring_format):
        scrape_calls["adp"] += 1
        return _adp_for_run(run["n"])

    async def auction_scraper(*, scoring_format, teams, roster):
        scrape_calls["auction"] += 1
        assert roster == CANONICAL_ROSTER  # flex-fixed roster threaded through
        return _auc_for_run(run["n"])

    recorded: list[dict] = []

    async def persist_fn(_session, rows):
        recorded.extend(rows)
        return len(rows)

    # --- Run 1 ---
    await ingest_format_market_data(
        session, adp_scraper=adp_scraper, auction_scraper=auction_scraper,
        persist_fn=persist_fn,
    )
    run1 = [r for r in recorded if r["scoring_format"] == "ppr"][-1]
    assert run1["adp_fantasypros"] == 2.0 and run1["auction_value"] == 55.0

    # --- Run 2 (source moved) ---
    recorded.clear()
    run["n"] = 2
    await ingest_format_market_data(
        session, adp_scraper=adp_scraper, auction_scraper=auction_scraper,
        persist_fn=persist_fn,
    )
    run2 = [r for r in recorded if r["scoring_format"] == "ppr"][-1]
    assert run2["adp_fantasypros"] == 5.0 and run2["auction_value"] == 40.0

    # Scrapers were awaited AGAIN on run 2 (3 formats × 2 runs = 6 each) — not memoized.
    assert scrape_calls["adp"] == 6 and scrape_calls["auction"] == 6
    assert session.commits == 2


@pytest.mark.asyncio
async def test_ingest_covers_all_three_formats():
    player = _p("Bijan Robinson", "RB")
    session = _FakeSession([player])
    seen_formats: list[str] = []

    async def adp_scraper(*, scoring_format):
        seen_formats.append(scoring_format)
        return [_adp("Bijan Robinson", "RB", 2)]

    async def auction_scraper(*, scoring_format, teams, roster):
        return [_auc("Bijan Robinson", "RB", 50.0)]

    async def persist_fn(_session, rows):
        return len(rows)

    result = await ingest_format_market_data(
        session, adp_scraper=adp_scraper, auction_scraper=auction_scraper,
        persist_fn=persist_fn,
    )
    assert set(seen_formats) == {"ppr", "half_ppr", "standard"}
    assert set(result["formats"].keys()) == {"ppr", "half_ppr", "standard"}
    assert result["roster_shape"] == CANONICAL_ROSTER_SHAPE
    assert result["teams"] == 12
