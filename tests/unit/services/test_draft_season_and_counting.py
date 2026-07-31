"""Two defects in league sync: the wrong draft season, and a counter that lied.

DEFECT 1 — the draft was filed under the wrong years.

`sync_league` in backend/services/league_sync.py looped over four past seasons and
asked the platform for each one. That only works for Yahoo, whose league key
carries a per-season game key (461.l.<id> is 2025, 423.l.<id> is 2023), so each
pass genuinely addresses a different draft.

ESPN and Sleeper ignore the league_key argument entirely. ESPN builds its URL from
the league's own season_year; a Sleeper league id IS one season. Both returned the
same current draft on every pass, so one draft was stored under 2025, 2024, 2023
and 2022 — and never under the year it belongs to. The wizard reported
"Seasons imported: 4" for a league that had drafted once.

DEFECT 2 — the pick counter reported attempts, not writes.

`_store_picks` incremented its counter unconditionally after an insert carrying
ON CONFLICT DO NOTHING, and incremented the resolved counter BEFORE the insert. So
a re-sync of an already-imported league reported a full healthy import while
writing nothing. The same number hides a genuine cross-tenant collision, where a
second customer's picks are silently discarded.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.league_sync import LeagueSyncService


def _pick(pid="p1", team="1"):
    return SimpleNamespace(
        platform_player_id=pid, player_name="Bijan Robinson", position="RB",
        auction_price=55, manager_name="", picked_by_team_id=team, pick_number=1,
    )


def _db(rowcounts):
    """A database whose inserts report the given rowcounts in order.

    rowcount 1 means the row was written; 0 means ON CONFLICT DO NOTHING
    suppressed it. Reproducing that distinction is the whole point — the existing
    test helper returns a MagicMock whose .rowcount is itself a MagicMock, and
    `MagicMock() != 0` is True, so every assertion passed without the suppressed
    path ever running.
    """
    db = MagicMock()
    seq = list(rowcounts)

    async def execute(stmt, *a, **kw):
        res = MagicMock()
        res.all.return_value = []
        sc = MagicMock()
        sc.all.return_value = []
        res.scalars.return_value = sc
        compiled = str(stmt)
        res.rowcount = seq.pop(0) if ("INSERT" in compiled.upper() and seq) else 1
        return res

    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    return db


# --- Defect 2: counting -----------------------------------------------------

@pytest.mark.asyncio
async def test_a_suppressed_insert_is_not_counted_as_imported():
    svc = LeagueSyncService(_db([0, 0, 0]), uuid.uuid4())
    out = await svc._store_picks(
        [_pick("a"), _pick("b"), _pick("c")], uuid.uuid4(), 2026, platform="espn")
    assert out["stored"] == 0, (
        "picks the database already had were reported as imported"
    )
    assert out["dropped"] == 3


@pytest.mark.asyncio
async def test_written_rows_are_counted():
    svc = LeagueSyncService(_db([1, 1]), uuid.uuid4())
    out = await svc._store_picks([_pick("a"), _pick("b")], uuid.uuid4(), 2026,
                                 platform="espn")
    assert (out["stored"], out["dropped"]) == (2, 0)


@pytest.mark.asyncio
async def test_a_mixed_batch_splits_correctly():
    svc = LeagueSyncService(_db([1, 0, 1, 0]), uuid.uuid4())
    out = await svc._store_picks(
        [_pick("a"), _pick("b"), _pick("c"), _pick("d")], uuid.uuid4(), 2026,
        platform="espn")
    assert (out["stored"], out["dropped"]) == (2, 2)


@pytest.mark.asyncio
async def test_resolved_never_exceeds_stored():
    """The caller divides one by the other to decide whether to warn that prices
    are unusable. If resolved counted attempts while stored counted writes, the
    ratio could exceed 1 on a re-sync and silence the warning exactly when it
    matters."""
    svc = LeagueSyncService(_db([0, 0]), uuid.uuid4())
    out = await svc._store_picks([_pick("a"), _pick("b")], uuid.uuid4(), 2026,
                                 platform="espn")
    assert out["resolved"] <= out["stored"]


# --- Defect 1: which seasons are requested ----------------------------------

def _sync_source() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[3]
            / "backend" / "services" / "league_sync.py").read_text(encoding="utf-8")


def test_only_yahoo_walks_back_through_past_seasons():
    """ESPN and Sleeper must import once, at the league's own season."""
    src = _sync_source()
    start = src.index("# 3. Import draft history")
    window = src[start:start + 2500]
    assert 'if user_league.platform == "yahoo"' in window, (
        "the draft import no longer branches on platform, so ESPN and Sleeper "
        "will be looped over past seasons again"
    )
    assert "seasons = [user_league.season_year]" in window, (
        "non-Yahoo platforms must import a single season, stamped with the "
        "league's own season_year"
    )


def test_the_non_yahoo_season_is_the_leagues_own_not_the_current_calendar_season():
    """ESPN's request URL is built from user_league.season_year. Stamping rows
    with anything else lets the stored year drift from the year fetched, and
    breaks deliberately connecting a past season."""
    src = _sync_source()
    start = src.index("# 3. Import draft history")
    window = src[start:start + 2500]
    assert "seasons = [get_current_season()]" not in window
    assert "seasons = [user_league.season_year]" in window
