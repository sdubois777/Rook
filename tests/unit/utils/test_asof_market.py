"""The as-of MARKET — sync_adp / format_market guards and _seed_asof_market.

Companion to test_seasons_asof.py, which covers the clock itself. This file covers the
one input the clock does NOT reach: the market columns.

WHY THIS MATTERS. Every signal the board emits is relative to "the market" —
value_gap = ai_bid_ceiling - market, and reconcile_value_signals() derives
value_assessment / pay_up_flag from that gap. The backtest then scores those signals
against what the as-of season's auction ACTUALLY paid. If the board's market is next
year's consensus while the scoring market is the past season's auction, the two sides
refer to different markets and the accuracy number measures nothing.

That is not hypothetical: on the 2026 board FantasyPros had Nico Collins at $31 and
Saquon Barkley at $31, while the real 2025 auction paid $62 and $61. A "we are $30 above
market" call scored against a $62 price is noise.

Both scrapes are LIVE and current-season only — FantasyPros publishes no historical ADP
and DraftWizard ignores the year parameter — so under an as-of clock there is nothing to
fetch and the only correct move is to skip and seed from market_value_historic instead.
"""
from __future__ import annotations

from datetime import date

import pytest

from backend.utils import seasons as S


@pytest.fixture
def asof(monkeypatch):
    """Set ROOK_ASOF_DATE for one test and guarantee it is removed afterwards."""
    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv(S.ASOF_ENV, raising=False)
        else:
            monkeypatch.setenv(S.ASOF_ENV, value)
    return _set


@pytest.fixture
def pipeline():
    """Load run_predraft_pipeline.py as a module (it is a script, not a package member)."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "run_predraft_pipeline.py"
    spec = importlib.util.spec_from_file_location("rpp_market_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# format_market — the per-format scrape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_format_market_stage_skipped_under_asof(asof, pipeline, capsys, monkeypatch):
    """The per-format ADP/auction scrape must not run under an as-of clock.

    It writes player_format_values, which is what the half-PPR and standard boards price
    against. Letting it run would put the CURRENT season's per-format market on a
    past-season board while PPR carried the correct one — the two formats would then
    disagree about what year it is.
    """
    from unittest.mock import AsyncMock

    ingest = AsyncMock(return_value={
        "formats": {}, "rows_written": 0, "roster_shape": "x",
    })
    monkeypatch.setattr(
        "backend.services.format_market_ingest.run_format_market_ingest_stage", ingest,
    )

    asof("2025-08-15")
    await pipeline.run_agent("format_market", None)
    ingest.assert_not_awaited()
    assert "SKIPPED" in capsys.readouterr().out

    # Real time must be completely unchanged — the guard is as-of only.
    asof(None)
    await pipeline.run_agent("format_market", None)
    ingest.assert_awaited_once()


# ---------------------------------------------------------------------------
# sync_adp — the FantasyPros ADP subprocess
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_adp_subprocess_not_spawned_under_asof(asof, pipeline, monkeypatch):
    """sync_adp is a subprocess.run of scripts/sync_adp.py — assert on the SPAWN.

    A Playwright scrape of live FantasyPros has no historical mode, so under as-of the
    process must never start. Asserting on the spawn (rather than on a print) is what
    makes this a real guard: it fails if someone reorders the skip below the call.
    """
    spawned = []

    def fake_run(cmd, *a, **kw):
        spawned.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    asof("2025-08-15")
    await _run_main_prelude(pipeline, monkeypatch)
    assert not any("sync_adp.py" in str(c) for c in spawned), (
        f"sync_adp was spawned under an as-of clock: {spawned}"
    )

    spawned.clear()
    asof(None)
    await _run_main_prelude(pipeline, monkeypatch)
    assert any("sync_adp.py" in str(c) for c in spawned), (
        "real-time runs must still sync ADP"
    )


async def _run_main_prelude(pipeline, monkeypatch):
    """Drive main() far enough to pass the sync_adp decision, then stop.

    main() is a long function whose tail builds the warehouse and runs every agent. We
    only care about the pre-agent stage ordering, so the warehouse build raises a
    sentinel that ends the run right after the decision under test.

    Driving the real function matters here: reimplementing the branch in the test would
    assert on the test's own copy of the logic, not the pipeline's.
    """
    import sys
    from unittest.mock import patch

    class _Stop(Exception):
        pass

    def stop(*a, **kw):
        raise _Stop()

    seeded = []
    monkeypatch.setattr(pipeline, "_seed_asof_market",
                        lambda: _noop(seeded.append("seeded")))
    monkeypatch.setattr("backend.db_guard.guard_writes", lambda *a, **kw: None)
    monkeypatch.setattr("backend.integrations.nfl_data.NflDataWarehouse.build", stop)

    argv = ["run_predraft_pipeline.py", "--agent", "team_systems", "--skip-seed"]
    with patch.object(sys, "argv", argv):
        try:
            await pipeline.main()
        except _Stop:
            pass


async def _noop(_):
    return None


# ---------------------------------------------------------------------------
# _seed_asof_market — the replacement market
# ---------------------------------------------------------------------------

def test_seed_asof_market_prices_from_the_asof_season(asof, pipeline, monkeypatch):
    """The seeded price must come from the AS-OF season, not the calendar season.

    _seed_asof_market resolves its year through get_current_season(), which is the
    as-of-aware clock. Binding it to the real year would copy a season the board is not
    built for — silently, because market_value_historic holds several seasons and any
    of them produces a plausible-looking board.
    """
    import asyncio

    captured = {}

    class _Result:
        rowcount = 159

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt, params=None):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _Result()

        async def commit(self):
            captured["committed"] = True

    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _Session())

    asof("2025-08-15")
    asyncio.run(pipeline._seed_asof_market())

    assert captured["params"] == {"yr": 2025}, (
        f"seeded the wrong season: {captured['params']}"
    )
    assert captured.get("committed") is True, "the update was never committed"

    sql = captured["sql"].lower()
    # Both market columns must move together. reconcile_value_signals reads the MAX of
    # league and fantasypros (PR #371), so seeding only one leaves the stale 2026 value
    # winning the max and silently reinstates the bug this function exists to fix.
    assert "market_value_league" in sql
    assert "market_value_fantasypros" in sql
    assert "market_value_historic" in sql
    # Never import a $0 row as a real market price — that would read as "free" and make
    # every ceiling look like a bargain.
    assert "price > 0" in sql


def test_seed_asof_market_year_tracks_the_clock(asof, pipeline, monkeypatch):
    """Same call, different clock, different season — the year is never hardcoded."""
    import asyncio

    years = []

    class _Result:
        rowcount = 0

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt, params=None):
            years.append(params["yr"])
            return _Result()

        async def commit(self):
            return None

    monkeypatch.setattr("backend.database.AsyncSessionLocal", lambda: _Session())

    asof("2023-08-15")
    asyncio.run(pipeline._seed_asof_market())
    asof("2025-08-15")
    asyncio.run(pipeline._seed_asof_market())
    asof(None)
    asyncio.run(pipeline._seed_asof_market())

    today = date.today()
    real = today.year if today.month >= 3 else today.year - 1
    assert years == [2023, 2025, real], f"season did not track the clock: {years}"


def test_seed_asof_market_runs_only_under_asof(pipeline):
    """A real-time run must never overwrite the live market with a historic auction.

    This is the failure direction that would corrupt PRODUCTION data rather than a
    throwaway backtest board, so it is asserted on the call site: _seed_asof_market must
    be reached only through an asof_active() branch.
    """
    import inspect
    import re

    src = inspect.getsource(pipeline.main)
    calls = [m for m in re.finditer(r"_seed_asof_market\(\)", src)]
    assert calls, "main() no longer calls _seed_asof_market"
    for m in calls:
        # The guard is the nearest preceding `if <something>asof_active...:` line.
        before = src[:m.start()]
        guard = [ln for ln in before.splitlines() if ln.strip().startswith("if ")][-1]
        assert "asof_active" in guard.replace("_asof_active", "asof_active"), (
            f"_seed_asof_market is not guarded by asof_active(); nearest if was: {guard!r}"
        )
