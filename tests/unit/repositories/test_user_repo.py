"""Tests for UserRepository — DB query layer.

These test the repository methods using mocked AsyncSession.
Integration tests with real DB are in tests/integration/.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.repositories.user_repo import UserRepository


def _make_session():
    session = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_get_by_external_id():
    session = _make_session()
    user = MagicMock()
    user.external_id = "clerk-user-123"

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = user
    session.execute.return_value = result_mock

    repo = UserRepository(session)
    found = await repo.get_by_external_id("clerk-user-123")

    assert found is user
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_by_external_id_not_found():
    session = _make_session()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute.return_value = result_mock

    repo = UserRepository(session)
    found = await repo.get_by_external_id("nonexistent")

    assert found is None


@pytest.mark.asyncio
async def test_update_credits_atomic():
    """update_credits returns new balance from DB."""
    session = _make_session()
    row = MagicMock()
    row.__getitem__ = lambda self, key: 40  # new balance

    result_mock = MagicMock()
    result_mock.fetchone.return_value = row
    session.execute.return_value = result_mock

    repo = UserRepository(session)
    user_id = uuid.uuid4()
    new_balance = await repo.update_credits(user_id, delta=-10)

    assert new_balance == 40
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_credits_cannot_go_below_zero():
    """When WHERE clause fails (insufficient credits), fetchone returns None."""
    session = _make_session()

    # First call: update returns None (WHERE clause failed)
    update_result = MagicMock()
    update_result.fetchone.return_value = None

    # Second call: get_or_404 → session.get returns user with current balance
    user = MagicMock()
    user.credits_remaining = 5

    session.execute.return_value = update_result
    session.get.return_value = user

    repo = UserRepository(session)
    user_id = uuid.uuid4()
    balance = await repo.update_credits(user_id, delta=-100)

    # Should return current balance (5), not negative
    assert balance == 5


@pytest.mark.asyncio
async def test_get_by_email():
    session = _make_session()
    user = MagicMock()
    user.email = "test@example.com"

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = user
    session.execute.return_value = result_mock

    repo = UserRepository(session)
    found = await repo.get_by_email("test@example.com")

    assert found is user


@pytest.mark.asyncio
async def test_add_monthly_credits():
    session = _make_session()

    # Simulate 3 users updated
    result_mock = MagicMock()
    result_mock.fetchall.return_value = [
        (uuid.uuid4(),), (uuid.uuid4(),), (uuid.uuid4(),)
    ]
    session.execute.return_value = result_mock

    repo = UserRepository(session)
    count = await repo.add_monthly_credits(tier="standard", monthly_amount=20)

    assert count == 3
