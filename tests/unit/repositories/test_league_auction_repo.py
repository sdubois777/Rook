"""Tests for LeagueAuctionHistoryRepository — auction history query layer.

Repository methods tested against a mocked AsyncSession; integration
tests with a real DB live in tests/integration/.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.repositories.league_auction_repo import (
    LeagueAuctionHistoryRepository,
)


def _make_session():
    return AsyncMock()


def _make_repo(session):
    return LeagueAuctionHistoryRepository(session)


@pytest.mark.asyncio
async def test_list_seasons_with_rows_returns_season_ints():
    """list_seasons unpacks single-column rows into a list of ints."""
    session = _make_session()
    result = MagicMock()
    result.all.return_value = [(2023,), (2024,), (2025,)]
    session.execute.return_value = result

    repo = _make_repo(session)
    seasons = await repo.list_seasons(uuid.uuid4(), uuid.uuid4())

    assert seasons == [2023, 2024, 2025]
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_seasons_with_no_history_returns_empty_list():
    """list_seasons returns [] when no history rows exist."""
    session = _make_session()
    result = MagicMock()
    result.all.return_value = []
    session.execute.return_value = result

    repo = _make_repo(session)
    seasons = await repo.list_seasons(uuid.uuid4(), uuid.uuid4())

    assert seasons == []


@pytest.mark.asyncio
async def test_position_trends_with_rows_passes_rows_through():
    """position_trends returns the aggregate rows from the query unchanged."""
    session = _make_session()
    rows = [MagicMock(), MagicMock()]
    result = MagicMock()
    result.all.return_value = rows
    session.execute.return_value = result

    repo = _make_repo(session)
    trends = await repo.position_trends(uuid.uuid4(), uuid.uuid4())

    assert trends == rows
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_tendencies_with_rows_passes_rows_through():
    """manager_tendencies returns the aggregate rows from the query unchanged."""
    session = _make_session()
    rows = [MagicMock()]
    result = MagicMock()
    result.all.return_value = rows
    session.execute.return_value = result

    repo = _make_repo(session)
    tendencies = await repo.manager_tendencies(uuid.uuid4(), uuid.uuid4())

    assert tendencies == rows
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_season_summaries_with_rows_passes_rows_through():
    """season_summaries returns the per-season aggregate rows unchanged."""
    session = _make_session()
    rows = [MagicMock(), MagicMock()]
    result = MagicMock()
    result.all.return_value = rows
    session.execute.return_value = result

    repo = _make_repo(session)
    summaries = await repo.season_summaries(uuid.uuid4(), uuid.uuid4())

    assert summaries == rows


@pytest.mark.asyncio
async def test_list_picks_with_rows_returns_records():
    """list_picks returns the ORM records for the requested season."""
    session = _make_session()
    picks = [MagicMock(), MagicMock()]
    scalars = MagicMock()
    scalars.all.return_value = picks
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result

    repo = _make_repo(session)
    records = await repo.list_picks(uuid.uuid4(), uuid.uuid4(), 2025)

    assert records == picks
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_for_league_issues_delete_without_commit():
    """delete_for_league executes a DELETE but never commits the session."""
    session = _make_session()

    repo = _make_repo(session)
    await repo.delete_for_league(uuid.uuid4(), uuid.uuid4())

    session.execute.assert_awaited_once()
    session.commit.assert_not_awaited()
