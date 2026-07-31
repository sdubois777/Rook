"""Connecting the same league twice must not dead-end the user.

THE BUG THESE EXIST FOR. `LeagueRepository.upsert` (backend/repositories/league_repo.py:149)
is idempotent and keyed on the real constraint, but only the YAHOO connect path
used it (league_connect.py:152-170). ESPN and Sleeper called
`LeagueService.add_league` -> `BaseRepository.create`, a bare `session.add` +
`flush` against a live UNIQUE (user_id, platform, league_id).

The user-visible failure was not a duplicate-row error, it was worse:
`count_active` counts the league the user ALREADY has, so on Standard
(max_leagues=1) `FeatureService.can_add_league` raised LeagueLimitError FIRST and
the user was told "League limit reached (1 of 1)" while owning exactly one league.
On Pro (max_leagues=None) the check passed and the INSERT died on the constraint
as an unmapped 500. Either way, reconnecting after an ESPN cookie expiry was
impossible.

It survived because every connect test in tests/unit/routers/test_league_connect.py
patches LeagueService, LeagueRepository, CredentialRepository, LeagueSyncService
AND FeatureService at once, so the constraint never executes. These tests run
against real Postgres and mock NONE of those.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.config import settings
from backend.models.user import User
from backend.models.user_league import UserLeague
from backend.repositories.league_repo import LeagueRepository
from backend.services.feature_service import FeatureService
from backend.core.exceptions import LeagueLimitError


@pytest.fixture
async def sm():
    engine = create_async_engine(settings.database_url, pool_size=2, max_overflow=1)
    try:
        async with engine.connect() as c:
            await asyncio.wait_for(c.execute(sa.select(1)), timeout=5)
    except Exception:
        await engine.dispose()
        pytest.skip("Postgres not reachable — skipping connect idempotency tests")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def user(sm):
    """A real users row, torn down with its leagues (FK cascade covers them)."""
    uid = uuid.uuid4()
    async with sm() as s:
        s.add(User(
            id=uid,
            external_id=f"user_test_{uid.hex[:12]}",
            email=f"{uid.hex[:12]}@example.test",
            tier="standard",
        ))
        await s.commit()
    yield uid
    async with sm() as s:
        await s.execute(sa.delete(UserLeague).where(UserLeague.user_id == uid))
        await s.execute(sa.delete(User).where(User.id == uid))
        await s.commit()


async def _set_tier(sm, uid, tier):
    async with sm() as s:
        await s.execute(sa.update(User).where(User.id == uid).values(tier=tier))
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["espn", "sleeper"])
async def test_reconnecting_the_same_league_creates_no_second_row(sm, user, platform):
    """The core guarantee. Two connects of the SAME league leave exactly one row."""
    async with sm() as s:
        repo = LeagueRepository(s)
        for _ in range(2):
            await repo.upsert(
                user_id=user, platform=platform, league_id="L1",
                season_year=2026, team_count=12, draft_type="auction",
                scoring="ppr", budget=200, is_active=True,
            )
        await s.commit()

    async with sm() as s:
        n = (await s.execute(
            sa.select(sa.func.count(UserLeague.id))
            .where(UserLeague.user_id == user, UserLeague.platform == platform)
        )).scalar()
    assert n == 1, f"reconnect created {n} rows; upsert is not idempotent"


@pytest.mark.asyncio
async def test_a_bare_insert_of_the_same_league_actually_violates_the_constraint(sm, user):
    """Proves the constraint is REAL and that `create` is the wrong tool.

    Without this, a reader could reasonably think the upsert change is cosmetic.
    It is not — the old path raises IntegrityError here.
    """
    async with sm() as s:
        s.add(UserLeague(
            user_id=user, platform="espn", league_id="DUP", season_year=2026,
            team_count=12, draft_type="auction", scoring="ppr", budget=200,
            is_active=True,
        ))
        await s.commit()

    with pytest.raises(sa.exc.IntegrityError):
        async with sm() as s:
            s.add(UserLeague(
                user_id=user, platform="espn", league_id="DUP", season_year=2026,
                team_count=12, draft_type="auction", scoring="ppr", budget=200,
                is_active=True,
            ))
            await s.commit()


@pytest.mark.asyncio
async def test_reconnect_updates_mutable_fields(sm, user):
    """A reconnect must refresh settings — that is usually WHY the user reconnected."""
    async with sm() as s:
        repo = LeagueRepository(s)
        await repo.upsert(
            user_id=user, platform="espn", league_id="L2", season_year=2026,
            team_count=10, draft_type="snake", scoring="standard", budget=None,
            is_active=True,
        )
        await s.commit()
    async with sm() as s:
        repo = LeagueRepository(s)
        await repo.upsert(
            user_id=user, platform="espn", league_id="L2", season_year=2026,
            team_count=12, draft_type="auction", scoring="ppr", budget=300,
            is_active=True,
        )
        await s.commit()

    async with sm() as s:
        row = (await s.execute(
            sa.select(UserLeague).where(
                UserLeague.user_id == user, UserLeague.league_id == "L2")
        )).scalar_one()
    assert (row.team_count, row.draft_type, row.scoring, row.budget) == (
        12, "auction", "ppr", 300)


@pytest.mark.asyncio
async def test_the_tier_cap_still_blocks_a_genuinely_new_league(sm, user):
    """The fix must not become a cap bypass. A DIFFERENT league still 403s."""
    async with sm() as s:
        repo = LeagueRepository(s)
        await repo.upsert(
            user_id=user, platform="espn", league_id="FIRST", season_year=2026,
            team_count=12, draft_type="auction", scoring="ppr", budget=200,
            is_active=True,
        )
        await s.commit()

    async with sm() as s:
        repo = LeagueRepository(s)
        u = (await s.execute(sa.select(User).where(User.id == user))).scalar_one()
        existing = await repo.find_by_identity(user, "espn", "SECOND")
        assert existing is None, "a different league_id must not resolve as existing"
        count = await repo.count_active(user)
        with pytest.raises(LeagueLimitError):
            FeatureService.can_add_league(u, count)


@pytest.mark.asyncio
async def test_reconnecting_at_the_cap_is_allowed(sm, user):
    """The exact case that produced 'League limit reached (1 of 1)' with one league.

    find_by_identity must resolve the existing league so the cap check is skipped.
    """
    async with sm() as s:
        repo = LeagueRepository(s)
        await repo.upsert(
            user_id=user, platform="espn", league_id="ONLY", season_year=2026,
            team_count=12, draft_type="auction", scoring="ppr", budget=200,
            is_active=True,
        )
        await s.commit()

    async with sm() as s:
        repo = LeagueRepository(s)
        existing = await repo.find_by_identity(user, "espn", "ONLY")
        assert existing is not None, (
            "find_by_identity did not resolve the user's own league — the cap "
            "check would fire and tell them they are at their limit"
        )
        assert await repo.count_active(user) == 1


@pytest.mark.asyncio
async def test_reactivating_a_dormant_league_cannot_bypass_the_cap(sm, user):
    """Gating the cap check purely on 'league already exists' opens a hole.

    A user at the cap with league A active can hold a SECOND league B that is
    is_active=False (a finished past season). Reconnecting B flips it active. If
    the cap check is skipped merely because B exists, the user ends up with two
    active leagues on a max_leagues=1 tier.

    count_active only counts is_active AND non-suspended
    (league_repo.py:67-73), so this asserts the shape the fix must respect.
    """
    async with sm() as s:
        repo = LeagueRepository(s)
        await repo.upsert(
            user_id=user, platform="espn", league_id="ACTIVE", season_year=2026,
            team_count=12, draft_type="auction", scoring="ppr", budget=200,
            is_active=True,
        )
        await repo.upsert(
            user_id=user, platform="espn", league_id="DORMANT", season_year=2024,
            team_count=12, draft_type="auction", scoring="ppr", budget=200,
            is_active=False,
        )
        await s.commit()

    async with sm() as s:
        repo = LeagueRepository(s)
        assert await repo.count_active(user) == 1, "only ACTIVE should count"
        existing = await repo.find_by_identity(user, "espn", "DORMANT")
        assert existing is not None and existing.is_active is False
        # The fix must re-check the cap when an EXISTING league is being
        # reactivated, not just when the league is new.
        u = (await s.execute(sa.select(User).where(User.id == user))).scalar_one()
        with pytest.raises(LeagueLimitError):
            FeatureService.can_add_league(u, await repo.count_active(user))


@pytest.mark.asyncio
async def test_pro_tier_reconnect_does_not_raise(sm, user):
    """max_leagues=None on pro — the path that 500'd on the constraint before."""
    await _set_tier(sm, user, "pro")
    async with sm() as s:
        repo = LeagueRepository(s)
        for _ in range(3):
            await repo.upsert(
                user_id=user, platform="sleeper", league_id="P1", season_year=2026,
                team_count=12, draft_type="auction", scoring="ppr", budget=200,
                is_active=True,
            )
        await s.commit()
    async with sm() as s:
        n = (await s.execute(
            sa.select(sa.func.count(UserLeague.id))
            .where(UserLeague.user_id == user)
        )).scalar()
    assert n == 1
