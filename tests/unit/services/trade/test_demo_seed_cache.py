"""
Per-process demo seed cache — the fix for the trade/waiver pages re-running the
full ~7-8s demo seed (159 fuzzy name resolutions + weekly build + evaluate_league)
on EVERY request. TEARDOWN with the rest of the demo scaffolding.

Semantics proven here:
  * repeat seeds return the SAME cached object and build exactly once;
  * an injected ``weekly_usage`` frame (the test path) BYPASSES the cache in both
    directions — it neither reads nor populates it;
  * clear_* drops the cache so the next seed rebuilds.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

import backend.services.trade.trade_demo_source as trade_mod
import backend.services.waiver.waiver_demo_source as waiver_mod


@pytest.fixture(autouse=True)
def _clean_caches():
    """Isolate every test (and the rest of the suite) from cache pollution."""
    trade_mod.clear_demo_seed_cache()
    waiver_mod.clear_waiver_seed_cache()
    yield
    trade_mod.clear_demo_seed_cache()
    waiver_mod.clear_waiver_seed_cache()


async def test_trade_seed_cached_and_built_once(monkeypatch):
    sentinel = object()
    build = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(trade_mod, "_seed_demo_league_uncached", build)

    first = await trade_mod.seed_demo_league(db=None)
    second = await trade_mod.seed_demo_league(db=None)

    assert first is sentinel and second is sentinel   # same shared instance
    assert build.await_count == 1                     # built exactly once


async def test_trade_injected_weekly_usage_bypasses_cache(monkeypatch):
    built = []

    async def build(db, weekly_usage, scoring_format="ppr"):
        obj = object()
        built.append(obj)
        return obj

    monkeypatch.setattr(trade_mod, "_seed_demo_league_uncached", build)
    frame = pd.DataFrame({"canonical_player_id": []})

    a = await trade_mod.seed_demo_league(db=None, weekly_usage=frame)
    b = await trade_mod.seed_demo_league(db=None, weekly_usage=frame)

    assert a is not b and len(built) == 2             # never cached
    assert not trade_mod._SEED_CACHE                  # never populated either


async def test_trade_clear_forces_rebuild(monkeypatch):
    build = AsyncMock(side_effect=[object(), object()])
    monkeypatch.setattr(trade_mod, "_seed_demo_league_uncached", build)

    first = await trade_mod.seed_demo_league(db=None)
    trade_mod.clear_demo_seed_cache()
    second = await trade_mod.seed_demo_league(db=None)

    assert first is not second and build.await_count == 2


async def test_waiver_seed_cached_and_built_once(monkeypatch):
    sentinel = object()
    build = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(waiver_mod, "_seed_demo_waiver_uncached", build)

    first = await waiver_mod.seed_demo_waiver(db=None)
    second = await waiver_mod.seed_demo_waiver(db=None)

    assert first is sentinel and second is sentinel
    assert build.await_count == 1


async def test_waiver_injected_weekly_usage_bypasses_cache(monkeypatch):
    built = []

    async def build(db, weekly_usage):
        obj = object()
        built.append(obj)
        return obj

    monkeypatch.setattr(waiver_mod, "_seed_demo_waiver_uncached", build)
    frame = pd.DataFrame({"canonical_player_id": []})

    a = await waiver_mod.seed_demo_waiver(db=None, weekly_usage=frame)
    b = await waiver_mod.seed_demo_waiver(db=None, weekly_usage=frame)

    assert a is not b and len(built) == 2
    assert not waiver_mod._WAIVER_SEED_CACHE
