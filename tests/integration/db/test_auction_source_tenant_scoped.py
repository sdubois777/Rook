"""One customer's draft picks must not overwrite another's.

The two unique constraints on league_auction_history are
(player_id, season_year, source) and (season_year, source, yahoo_player_key).
Neither carries a customer or league column, so the `source` value is the only
thing separating one customer's draft from another's.

The sync used to write "sync_{picked_by_team_id}". On ESPN that team id is
"1".."12" and on Sleeper it is a roster id in the same range, so two customers
produced identical values — and the insert carries ON CONFLICT DO NOTHING, so the
second customer's picks were silently discarded with no error anywhere.

Yahoo looked immune because its team id is a full team key containing the league
id. It is not: two customers in the SAME Yahoo league produce identical values
too. Production already has this — two different users both hold Yahoo league
141688.

The value now embeds the user_league row id, which is unique per customer.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import settings
from backend.services.league_sync import LeagueSyncService


@pytest.fixture
async def sm():
    engine = create_async_engine(settings.database_url, pool_size=2, max_overflow=1)
    try:
        async with engine.connect() as c:
            await asyncio.wait_for(c.execute(sa.select(1)), timeout=5)
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable — skipping source scoping tests")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_source_column_can_hold_a_tenant_scoped_value(sm):
    """A Yahoo team key plus a UUID needs 58 characters, measured against
    production. The column held 50, so this would have raised
    StringDataRightTruncation on every Yahoo league sync."""
    async with sm() as s:
        width = (await s.execute(sa.text("""
            SELECT character_maximum_length FROM information_schema.columns
            WHERE table_name = 'league_auction_history' AND column_name = 'source'
        """))).scalar()
    needed = len(f"sync_{uuid.uuid4()}_470.l.141688.t.12")
    assert width >= needed, (
        f"source is VARCHAR({width}) but a tenant-scoped value needs {needed}"
    )


def _pick(team_id="3", player="p1"):
    return SimpleNamespace(
        platform_player_id=player, player_name="Bijan Robinson", position="RB",
        auction_price=55, manager_name="", picked_by_team_id=team_id, pick_number=1,
    )


def _capturing_db():
    db = MagicMock()
    captured: list = []

    async def execute(stmt, *a, **kw):
        try:
            params = stmt.compile().params
            if "source" in params:
                captured.append(params["source"])
        except Exception:
            pass
        res = MagicMock()
        res.rowcount = 1
        res.all.return_value = []
        sc = MagicMock()
        sc.all.return_value = []
        res.scalars.return_value = sc
        return res

    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    db.captured = captured
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,team_id",
    [("espn", "3"), ("sleeper", "3"), ("yahoo", "470.l.141688.t.3")],
)
async def test_two_customers_produce_different_source_values(platform, team_id):
    """The core guarantee, on every platform.

    Yahoo is included deliberately: it was assumed safe because its team id
    contains the league id, but two customers in the SAME league defeat that.
    """
    sources = []
    for _ in range(2):
        db = _capturing_db()
        svc = LeagueSyncService(db, uuid.uuid4())
        await svc._store_picks(
            [_pick(team_id)], uuid.uuid4(), 2026, platform=platform)
        sources.append(db.captured[0])

    assert sources[0] != sources[1], (
        f"two customers on {platform} produced the identical source value "
        f"{sources[0]!r}; the second one's picks would be silently discarded"
    )


@pytest.mark.asyncio
async def test_the_source_value_contains_the_league_id():
    db = _capturing_db()
    league_id = uuid.uuid4()
    svc = LeagueSyncService(db, uuid.uuid4())
    await svc._store_picks([_pick("3")], league_id, 2026, platform="espn")
    assert str(league_id) in db.captured[0], (
        f"source {db.captured[0]!r} does not identify the league"
    )


@pytest.mark.asyncio
async def test_the_same_customer_still_deduplicates():
    """Scoping must not break re-sync deduplication. The same customer syncing
    the same pick twice must produce the SAME value, so the second insert is
    correctly recognised as already present."""
    league_id = uuid.uuid4()
    user_id = uuid.uuid4()
    sources = []
    for _ in range(2):
        db = _capturing_db()
        svc = LeagueSyncService(db, user_id)
        await svc._store_picks([_pick("3")], league_id, 2026, platform="espn")
        sources.append(db.captured[0])
    assert sources[0] == sources[1]
