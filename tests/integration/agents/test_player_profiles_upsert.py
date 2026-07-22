"""Integration tests for the player_profiles atomic upsert (race fix).

The profiles phase writes teams CONCURRENTLY (asyncio.gather, own session per team). Two
teams resolving the SAME player_id (a dual-rostered / traded player) previously both
INSERTed → the loser hit `uq_player_profiles_player_id`. _upsert_profile replaces that
check-then-insert with INSERT ... ON CONFLICT (player_id) DO UPDATE.

These exercise the real Postgres ON CONFLICT — they need a reachable DB (skipped otherwise)
and never run against prod (conftest kill-switch). Each test uses a FRESH function-scoped
engine so all work happens in that test's own event loop (pytest-asyncio gives each test a
new loop; sharing the module-level pool across loops otherwise trips asyncpg).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import settings
from backend.models.player import Player, PlayerProfile
from backend.agents.player_profiles import _upsert_profile


@pytest.fixture
async def sm():
    """Fresh async_sessionmaker on a dedicated engine in this test's loop; skip if no DB."""
    engine = create_async_engine(settings.database_url, pool_size=3, max_overflow=2)
    try:
        async with engine.connect() as c:
            await asyncio.wait_for(c.execute(select(1)), timeout=5)
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable — skipping player_profiles upsert tests")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _profile(pid: uuid.UUID, role: str, ppr: int) -> PlayerProfile:
    rec = PlayerProfile(player_id=pid, season_year=2026)
    rec.role_classification = role
    rec.clean_season_baseline = {"ppr_points": ppr, "prompt_version": "test"}
    rec.is_rookie = False
    rec.profile_source = "nfl_history"
    rec.confidence = "medium"
    return rec


async def _make_player(sm) -> uuid.UUID:
    pid = uuid.uuid4()
    async with sm() as s:
        s.add(Player(id=pid, name=f"UPSERT_TEST_{pid.hex[:8]}", position="WR", team_abbr="TST"))
        await s.commit()
    return pid


async def _count(sm, pid: uuid.UUID) -> int:
    async with sm() as s:
        rows = (await s.execute(
            select(PlayerProfile).where(PlayerProfile.player_id == pid)
        )).scalars().all()
        return len(rows)


async def _cleanup(sm, pid: uuid.UUID) -> None:
    async with sm() as s:
        await s.execute(delete(PlayerProfile).where(PlayerProfile.player_id == pid))
        await s.execute(delete(Player).where(Player.id == pid))
        await s.commit()


@pytest.mark.asyncio
async def test_concurrent_upsert_same_player_no_duplicate(sm):
    """Two CONCURRENT upserts of the same player_id must NOT raise a duplicate-key and must
    leave exactly ONE row — the race that produced uq_player_profiles_player_id."""
    pid = await _make_player(sm)
    err: Exception | None = None
    try:
        async def writer(role: str, ppr: int):
            async with sm() as s:
                await _upsert_profile(s, _profile(pid, role, ppr))
                await s.commit()
        try:
            await asyncio.gather(writer("wr1_alpha", 260), writer("wr2", 180))
        except Exception as e:
            err = e
        n = await _count(sm, pid)
    finally:
        await _cleanup(sm, pid)

    assert err is None, f"concurrent upsert raised (the pre-fix race): {err!r}"
    assert n == 1


@pytest.mark.asyncio
async def test_profile_phase_idempotent_no_duplicate_key(sm):
    """Writing the same player's profile TWICE (separate committed txns — like running the
    profile phase twice) leaves one row, never a duplicate-key, last-write-wins."""
    pid = await _make_player(sm)
    try:
        async with sm() as s:
            await _upsert_profile(s, _profile(pid, "wr1_alpha", 260))
            await s.commit()
        async with sm() as s:                    # deterministically hits ON CONFLICT
            await _upsert_profile(s, _profile(pid, "wr2", 175))
            await s.commit()

        assert await _count(sm, pid) == 1
        async with sm() as s:
            row = (await s.execute(
                select(PlayerProfile).where(PlayerProfile.player_id == pid)
            )).scalar_one()
            assert row.role_classification == "wr2"                       # last write wins
            assert row.clean_season_baseline["ppr_points"] == 175
    finally:
        await _cleanup(sm, pid)
