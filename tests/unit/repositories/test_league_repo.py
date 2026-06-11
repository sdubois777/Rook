"""Tests for LeagueRepository — user league query layer.

Repository methods tested against a mocked AsyncSession; integration
tests with a real DB live in tests/integration/.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.repositories.league_repo import LeagueRepository


def _make_session():
    return AsyncMock()


@pytest.mark.asyncio
async def test_get_by_user_with_leagues_returns_all():
    """get_by_user returns every league row for the user."""
    session = _make_session()
    leagues = [MagicMock(), MagicMock()]

    scalars = MagicMock()
    scalars.all.return_value = leagues
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result

    repo = LeagueRepository(session)
    found = await repo.get_by_user(uuid.uuid4())

    assert found == leagues
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_by_user_with_no_leagues_returns_empty_list():
    """get_by_user returns [] when the user has no leagues."""
    session = _make_session()
    scalars = MagicMock()
    scalars.all.return_value = []
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result

    repo = LeagueRepository(session)
    found = await repo.get_by_user(uuid.uuid4())

    assert found == []


@pytest.mark.asyncio
async def test_get_user_league_when_owned_returns_league():
    """get_user_league returns the league when the user owns it."""
    session = _make_session()
    league = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = league
    session.execute.return_value = result

    repo = LeagueRepository(session)
    found = await repo.get_user_league(uuid.uuid4(), uuid.uuid4())

    assert found is league
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_user_league_when_ownership_mismatch_returns_none():
    """get_user_league returns None when the row belongs to another user."""
    session = _make_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    repo = LeagueRepository(session)
    found = await repo.get_user_league(uuid.uuid4(), uuid.uuid4())

    assert found is None


@pytest.mark.asyncio
async def test_find_by_identity_when_found_returns_league():
    """find_by_identity returns the league matching (user, platform, league_id)."""
    session = _make_session()
    league = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = league
    session.execute.return_value = result

    repo = LeagueRepository(session)
    found = await repo.find_by_identity(uuid.uuid4(), "yahoo", "12345")

    assert found is league


@pytest.mark.asyncio
async def test_find_by_identity_when_missing_returns_none():
    """find_by_identity returns None when no league matches the identity key."""
    session = _make_session()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    repo = LeagueRepository(session)
    found = await repo.find_by_identity(uuid.uuid4(), "espn", "nope")

    assert found is None


@pytest.mark.asyncio
async def test_count_active_with_active_leagues_returns_count():
    """count_active returns the scalar count of active leagues."""
    session = _make_session()
    result = MagicMock()
    result.scalar.return_value = 2
    session.execute.return_value = result

    repo = LeagueRepository(session)
    count = await repo.count_active(uuid.uuid4())

    assert count == 2


@pytest.mark.asyncio
async def test_count_active_with_none_scalar_returns_zero():
    """count_active returns 0 when the count query yields None."""
    session = _make_session()
    result = MagicMock()
    result.scalar.return_value = None
    session.execute.return_value = result

    repo = LeagueRepository(session)
    count = await repo.count_active(uuid.uuid4())

    assert count == 0


@pytest.mark.asyncio
async def test_get_user_leagues_by_platform_returns_platform_leagues():
    """get_user_leagues_by_platform returns active leagues for one platform."""
    session = _make_session()
    leagues = [MagicMock()]
    scalars = MagicMock()
    scalars.all.return_value = leagues
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result

    repo = LeagueRepository(session)
    found = await repo.get_user_leagues_by_platform(uuid.uuid4(), "sleeper")

    assert found == leagues


@pytest.mark.asyncio
async def test_upsert_returns_row_and_flushes():
    """upsert returns the row from RETURNING and flushes the session."""
    session = _make_session()
    league = MagicMock()
    result = MagicMock()
    result.scalar_one.return_value = league
    session.execute.return_value = result

    repo = LeagueRepository(session)
    saved = await repo.upsert(
        uuid.uuid4(),
        "yahoo",
        "461.l.12345",
        season_year=2026,
        team_count=12,
        draft_type="auction",
        scoring="ppr",
        budget=200,
    )

    assert saved is league
    session.execute.assert_awaited_once()
    session.flush.assert_awaited_once()
