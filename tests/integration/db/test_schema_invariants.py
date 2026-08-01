"""Database-level invariants that a mocked session cannot observe.

These exist because CI ran `tests/unit/` against a DATABASE_URL with nothing
listening, so every constraint, ON CONFLICT and column width in the schema was
unverifiable. A dozen defects reached production through that gap — the most
expensive being that ESPN/Sleeper league connect is a bare INSERT against a live
unique constraint, which no test could see because `LeagueRepository` was mocked
in every connect test.

Each assertion here is one a MagicMock would satisfy trivially and Postgres does
not. Keep them cheap and schema-level; behavioural tests belong beside their
feature.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import settings


@pytest.fixture
async def sm():
    """Fresh engine in this test's own loop; skip when no Postgres is reachable.

    Function-scoped for the same reason as tests/integration/agents: pytest-asyncio
    gives each test a new loop and a shared asyncpg pool across loops trips.
    """
    engine = create_async_engine(settings.database_url, pool_size=2, max_overflow=1)
    try:
        async with engine.connect() as c:
            await asyncio.wait_for(c.execute(sa.select(1)), timeout=5)
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable — skipping schema invariant tests")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_leagues_unique_constraint_is_deployed(sm):
    """(user_id, platform, league_id) must be unique.

    This is the constraint the ESPN and Sleeper connect endpoints violate: they
    INSERT unconditionally where Yahoo upserts. Pinning it here means a future
    migration cannot quietly drop it and make the connect bug undetectable again.
    """
    async with sm() as s:
        rows = (await s.execute(sa.text("""
            SELECT con.conname, array_agg(att.attname ORDER BY att.attname) AS cols
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN unnest(con.conkey) AS k(attnum) ON TRUE
            JOIN pg_attribute att ON att.attrelid = rel.oid AND att.attnum = k.attnum
            WHERE rel.relname = 'user_leagues' AND con.contype = 'u'
            GROUP BY con.conname
        """))).all()
    combos = [tuple(r.cols) for r in rows]
    assert ("league_id", "platform", "user_id") in combos, (
        f"user_leagues unique constraint missing; found {combos}"
    )


@pytest.mark.asyncio
async def test_auction_source_column_fits_a_tenant_scoped_value(sm):
    """`source` must hold "sync_{user_league_id}_{team_id}".

    The value that separates one customer's draft picks from another's embeds the
    user_league row id. A Yahoo team key is itself about 18 characters, so the
    composed value reaches 58 — it did not fit the original 50-character column
    and raised StringDataRightTruncation on every Yahoo sync.

    The column was widened by migration src2026tenant. This now guards against a
    future migration narrowing it again, which would break every Yahoo league
    sync at the moment a customer connects one.
    """
    async with sm() as s:
        width = (await s.execute(sa.text("""
            SELECT character_maximum_length FROM information_schema.columns
            WHERE table_name = 'league_auction_history' AND column_name = 'source'
        """))).scalar()
    needed = len(f"sync_{uuid.uuid4()}_461.l.78512.t.12")
    assert width is not None, "source should be a bounded varchar"
    assert width >= needed, (
        f"league_auction_history.source is VARCHAR({width}) but a tenant-scoped "
        f"value needs {needed}. Widen it before scoping the dedupe key."
    )


@pytest.mark.asyncio
async def test_opponent_profiles_is_tenant_scoped(sm):
    """opponent_profiles had no tenant column at all while being read on every
    user's live-draft path. Both columns must stay NOT NULL so a future writer
    cannot insert an unowned row."""
    async with sm() as s:
        rows = (await s.execute(sa.text("""
            SELECT column_name, is_nullable FROM information_schema.columns
            WHERE table_name = 'opponent_profiles'
              AND column_name IN ('user_id', 'user_league_id')
        """))).all()
    got = {r.column_name: r.is_nullable for r in rows}
    assert got == {"user_id": "NO", "user_league_id": "NO"}, (
        f"opponent_profiles tenancy regressed: {got}"
    )


@pytest.mark.asyncio
async def test_on_conflict_do_nothing_reports_rowcount_zero(sm):
    """The behaviour `_store_picks` miscounts.

    `count += 1` fires unconditionally after an ON CONFLICT DO NOTHING insert, so
    a re-sync reports a full healthy import while writing nothing. The existing
    unit test passes only because MagicMock().rowcount != 0 is True — this pins
    the real semantics so the fix can be asserted against something true.
    """
    async with sm() as s:
        await s.execute(sa.text("""
            CREATE TEMP TABLE _rowcount_probe (
                id int PRIMARY KEY, val text
            ) ON COMMIT DROP
        """))
        first = await s.execute(sa.text(
            "INSERT INTO _rowcount_probe (id, val) VALUES (1, 'a') "
            "ON CONFLICT DO NOTHING"))
        second = await s.execute(sa.text(
            "INSERT INTO _rowcount_probe (id, val) VALUES (1, 'b') "
            "ON CONFLICT DO NOTHING"))
        assert first.rowcount == 1, "a fresh insert must report 1"
        assert second.rowcount == 0, (
            "a suppressed conflict must report 0 — this is the signal "
            "_store_picks ignores"
        )
        await s.rollback()
