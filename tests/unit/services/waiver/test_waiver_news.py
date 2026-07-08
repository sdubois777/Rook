"""News tie-in — depth-chart next-man-up resolution + signal-type constants +
the DIRECT-signal polarity gate."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.waiver.news_tiein import (
    DIRECT_POSITIVE_TYPES,
    OPPORTUNITY_TYPES,
    build_news_map,
    _next_up,
)

# (depth_chart_order, player_id, name) sorted by order — the universal backbone.
CHAIN = [(1, "starter", "Starter"), (2, "backup", "Backup"), (3, "third", "Third")]


def test_next_up_returns_the_player_below_the_starter():
    assert _next_up(CHAIN, "starter") == ("backup", "Backup")
    assert _next_up(CHAIN, "backup") == ("third", "Third")


def test_next_up_none_when_last_or_absent():
    assert _next_up(CHAIN, "third") is None          # already last
    assert _next_up(CHAIN, "not-in-chain") is None    # unknown starter


def test_signal_type_partitions_are_disjoint_and_expected():
    # Opportunity (surfaces a backup) vs direct-positive (the pool player himself).
    assert "injury_flag" in OPPORTUNITY_TYPES
    assert "depth_chart_change" in OPPORTUNITY_TYPES
    assert "camp_standout" in DIRECT_POSITIVE_TYPES
    assert "transaction" in DIRECT_POSITIVE_TYPES
    # A self injury_flag / practice_limited is SUPPRESSIVE — must NOT be a direct badge.
    assert "injury_flag" not in DIRECT_POSITIVE_TYPES
    assert "practice_limited" not in DIRECT_POSITIVE_TYPES


# --- DIRECT-signal polarity gate (the bug fix) -----------------------------

def _sig(pid, signal_type):
    return SimpleNamespace(
        player_id=pid, signal_type=signal_type, raw_text=f"{pid} {signal_type} news",
        confidence="high", source="rss", flagged_at=None,
    )


def _row(sig, name, team, pos, injury_status=None):
    """Mirror NewsRepository.list_feed's REAL row shape so build_news_map's unpacking
    is guarded against shape drift (a 5th column, injury_status, was added in #230 and
    broke the 4-value unpack — this keeps the test's rows shaped like production):
    (BeatReporterSignal, player_name, player_team, player_position, player_injury_status)."""
    return (sig, name, team, pos, injury_status)


async def _build(rows, pool_ids):
    """Run build_news_map over mocked signal rows with the DB helpers stubbed out
    (no depth chart / contingents needed to exercise the DIRECT gate)."""
    fake_repo = MagicMock()
    fake_repo.list_feed = AsyncMock(return_value=(rows, len(rows)))
    with patch("backend.services.waiver.news_tiein.NewsRepository", return_value=fake_repo), \
         patch("backend.services.waiver.news_tiein._depth_chart_map", AsyncMock(return_value={})), \
         patch("backend.services.waiver.news_tiein._names_for", AsyncMock(return_value={})), \
         patch("backend.services.waiver.news_tiein._contingent_map", AsyncMock(return_value={})):
        return await build_news_map(None, pool_ids, now=datetime(2026, 1, 1, tzinfo=timezone.utc))


async def test_self_injury_flag_attaches_no_direct_news():
    # An add candidate's OWN injury_flag must not decorate his card (and therefore
    # must not rescue/up-rank him downstream — recommendations key off news presence).
    rows = [_row(_sig("p_inj", "injury_flag"), "Inj Guy", "SF", "WR")]
    news = await _build(rows, {"p_inj"})
    assert "p_inj" not in news


async def test_self_practice_limited_attaches_no_direct_news():
    rows = [_row(_sig("p_lim", "practice_limited"), "Lim Guy", "KC", "RB")]
    news = await _build(rows, {"p_lim"})
    assert "p_lim" not in news


async def test_positive_direct_signals_still_attach():
    rows = [
        _row(_sig("p_camp", "camp_standout"), "Camp Guy", "MIN", "WR"),
        _row(_sig("p_txn", "transaction"), "Txn Guy", "NYJ", "RB"),
    ]
    news = await _build(rows, {"p_camp", "p_txn"})
    assert news["p_camp"].kind == "direct" and news["p_camp"].signal_type == "camp_standout"
    assert news["p_txn"].kind == "direct" and news["p_txn"].signal_type == "transaction"


async def test_gate_falls_through_to_a_positive_signal():
    # A player with BOTH a negative and a positive signal surfaces on the positive.
    rows = [
        _row(_sig("p_both", "injury_flag"), "Both Guy", "DET", "TE"),      # newest, negative → skipped
        _row(_sig("p_both", "camp_standout"), "Both Guy", "DET", "TE"),    # positive → attaches
    ]
    news = await _build(rows, {"p_both"})
    assert news["p_both"].kind == "direct" and news["p_both"].signal_type == "camp_standout"
