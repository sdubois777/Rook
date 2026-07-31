"""The connect ENDPOINTS must be idempotent — tested through HTTP against real Postgres.

tests/integration/db/test_connect_idempotency.py proves `LeagueRepository.upsert`
is idempotent. It already was. The defect is that only the Yahoo ROUTER used it;
ESPN and Sleeper called `LeagueService.add_league` -> a bare INSERT.

So the bug lives precisely in the gap between those two files, and only a test
that drives the real endpoint against a real database can see it. Every existing
connect test patches LeagueService, LeagueRepository, CredentialRepository,
LeagueSyncService AND FeatureService simultaneously
(tests/unit/routers/test_league_connect.py), which is why this shipped.

What is mocked here: the PLATFORM (Sleeper's public HTTP API, ESPN's cookie
validation) and the league SYNC, because neither should reach the network in a
test. What is NOT mocked: the database, LeagueRepository, LeagueService,
FeatureService, CredentialRepository — every layer the bug lives in.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import settings
from backend.core.dependencies import get_current_user, get_db
from backend.main import app
from backend.models.user import User
from backend.models.user_league import UserLeague


@pytest.fixture
async def engine():
    eng = create_async_engine(settings.database_url, pool_size=3, max_overflow=2)
    try:
        async with eng.connect() as c:
            await asyncio.wait_for(c.execute(sa.select(1)), timeout=5)
    except Exception:
        await eng.dispose()
        pytest.skip("Postgres not reachable — skipping connect endpoint tests")
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def ctx(engine):
    """A real user + real DB wired into the app's dependencies."""
    sm = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    async with sm() as s:
        s.add(User(
            id=uid,
            external_id=f"user_ep_{uid.hex[:12]}",
            email=f"ep_{uid.hex[:12]}@example.test",
            tier="standard",
        ))
        await s.commit()

    async def _get_db():
        async with sm() as s:
            yield s

    async def _get_user():
        async with sm() as s:
            return (await s.execute(
                sa.select(User).where(User.id == uid))).scalar_one()

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _get_user
    try:
        yield {"uid": uid, "sm": sm}
    finally:
        app.dependency_overrides.clear()
        async with sm() as s:
            await s.execute(sa.delete(UserLeague).where(UserLeague.user_id == uid))
            await s.execute(sa.delete(User).where(User.id == uid))
            await s.commit()


async def _league_rows(sm, uid):
    async with sm() as s:
        return (await s.execute(
            sa.select(UserLeague).where(UserLeague.user_id == uid)
        )).scalars().all()


@pytest.mark.asyncio
async def test_connecting_the_same_sleeper_league_twice_succeeds(ctx):
    """THE REGRESSION TEST.

    Second call today: `count_active` returns 1, tier standard caps at 1, so
    FeatureService raises LeagueLimitError -> 403 "League limit reached (1 of 1)"
    for a user who owns exactly one league. This asserts it does not.
    """
    sleeper_user = {"user_id": "sleeper-123", "username": "tester"}

    class _Resp:
        status_code = 200
        def json(self):
            return sleeper_user

    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_Resp())), \
            patch("backend.services.league_sync.LeagueSyncService.sync_league",
                  AsyncMock(return_value={"picks_imported": 0, "seasons_imported": 0})):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            body = {"username": "tester", "league_id": "SLEEP1"}
            first = await ac.post("/api/leagues/connect/sleeper", json=body)
            second = await ac.post("/api/leagues/connect/sleeper", json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, (
        f"reconnecting the same Sleeper league returned {second.status_code}: "
        f"{second.text}"
    )
    rows = await _league_rows(ctx["sm"], ctx["uid"])
    assert len(rows) == 1, f"expected 1 league row, got {len(rows)}"


@pytest.mark.asyncio
async def test_the_limit_error_is_not_raised_for_a_league_the_user_owns(ctx):
    """Pins the SYMPTOM, not just the row count.

    The message the user actually saw is rendered verbatim by
    frontend/src/pages/LeagueSetup.jsx, so assert the wording never comes back
    for a reconnect.
    """
    sleeper_user = {"user_id": "sleeper-123", "username": "tester"}

    class _Resp:
        status_code = 200
        def json(self):
            return sleeper_user

    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_Resp())), \
            patch("backend.services.league_sync.LeagueSyncService.sync_league",
                  AsyncMock(return_value={})):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            body = {"username": "tester", "league_id": "SLEEP2"}
            await ac.post("/api/leagues/connect/sleeper", json=body)
            second = await ac.post("/api/leagues/connect/sleeper", json=body)

    assert "limit" not in second.text.lower(), (
        f"reconnect surfaced a limit error: {second.text}"
    )


@pytest.mark.asyncio
async def test_a_genuinely_new_second_league_is_still_capped(ctx):
    """The cap must survive the fix — a DIFFERENT league_id still 403s."""
    sleeper_user = {"user_id": "sleeper-123", "username": "tester"}

    class _Resp:
        status_code = 200
        def json(self):
            return sleeper_user

    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_Resp())), \
            patch("backend.services.league_sync.LeagueSyncService.sync_league",
                  AsyncMock(return_value={})):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            await ac.post("/api/leagues/connect/sleeper",
                          json={"username": "tester", "league_id": "FIRST"})
            other = await ac.post("/api/leagues/connect/sleeper",
                                  json={"username": "tester", "league_id": "SECOND"})

    assert other.status_code == 403, (
        f"a second DISTINCT league must be capped, got {other.status_code}"
    )
    rows = await _league_rows(ctx["sm"], ctx["uid"])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_failed_sync_leaves_a_league_the_user_can_retry(ctx):
    """The orphan case.

    league_service commits the UserLeague BEFORE sync runs, so a sync failure
    leaves a committed row while the user sees an error. Their natural retry then
    hit the connect bug. Assert the retry now works and does not duplicate.
    """
    sleeper_user = {"user_id": "sleeper-123", "username": "tester"}

    class _Resp:
        status_code = 200
        def json(self):
            return sleeper_user

    body = {"username": "tester", "league_id": "FLAKY"}
    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_Resp())), \
            patch("backend.services.league_sync.LeagueSyncService.sync_league",
                  AsyncMock(side_effect=RuntimeError("sleeper 503"))):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            await ac.post("/api/leagues/connect/sleeper", json=body)

    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_Resp())), \
            patch("backend.services.league_sync.LeagueSyncService.sync_league",
                  AsyncMock(return_value={"picks_imported": 5})):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            retry = await ac.post("/api/leagues/connect/sleeper", json=body)

    assert retry.status_code == 200, (
        f"retry after a failed sync must succeed, got {retry.status_code}: {retry.text}"
    )
    rows = await _league_rows(ctx["sm"], ctx["uid"])
    assert len(rows) == 1, f"retry duplicated the league: {len(rows)} rows"


@pytest.mark.asyncio
async def test_connect_returns_the_league_UUID_not_the_platform_id(ctx):
    """The response's league_id must be the UserLeague UUID.

    sync_league's summary carries its OWN "league_id" — the PLATFORM id string —
    and it was spread LAST, clobbering the UUID. adoptLeague matches this against
    `l.id` from /account/leagues (a UUID), so it never matched and the app stayed
    pointed at the previously selected league: wrong name in the sidebar, wrong
    format on every board. Affected all three platforms.
    """
    sleeper_user = {"user_id": "sleeper-123", "username": "tester"}

    class _Resp:
        status_code = 200
        def json(self):
            return sleeper_user

    with patch("httpx.AsyncClient.get", AsyncMock(return_value=_Resp())), \
            patch("backend.services.league_sync.LeagueSyncService.sync_league",
                  AsyncMock(return_value={"league_id": "PLATFORM-999",
                                          "platform": "sleeper"})):
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://test") as ac:
            resp = await ac.post("/api/leagues/connect/sleeper",
                                 json={"username": "tester", "league_id": "UUIDCHK"})

    assert resp.status_code == 200, resp.text
    returned = resp.json()["league_id"]
    assert returned != "PLATFORM-999", (
        "the summary's platform league_id clobbered the UUID — adoptLeague "
        "cannot match this and the app will stay on the wrong league"
    )
    rows = await _league_rows(ctx["sm"], ctx["uid"])
    assert returned == str(rows[0].id)
