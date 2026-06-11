"""Tests for CreditRepository — credit ledger query layer.

Repository methods tested against a mocked AsyncSession; integration
tests with a real DB live in tests/integration/.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.user import CreditUsageLog
from backend.repositories.credit_repo import CreditRepository


def _make_session():
    session = AsyncMock()
    session.add = MagicMock()  # session.add is synchronous
    return session


@pytest.mark.asyncio
async def test_log_usage_with_full_args_adds_and_flushes_row():
    """log_usage adds a CreditUsageLog to the session, flushes, and returns it."""
    session = _make_session()
    repo = CreditRepository(session)
    user_id = uuid.uuid4()

    row = await repo.log_usage(
        user_id,
        action="trade_analysis",
        credits_used=10,
        agent_name="trade_analyzer",
        cost_usd=0.42,
    )

    assert isinstance(row, CreditUsageLog)
    assert row.user_id == user_id
    assert row.action == "trade_analysis"
    assert row.credits_used == 10
    assert row.agent_name == "trade_analyzer"
    assert row.cost_usd == 0.42
    session.add.assert_called_once_with(row)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_log_usage_with_minimal_args_defaults_optionals_to_none():
    """log_usage leaves agent_name and cost_usd as None when omitted."""
    session = _make_session()
    repo = CreditRepository(session)

    row = await repo.log_usage(uuid.uuid4(), action="waiver_scan", credits_used=8)

    assert row.agent_name is None
    assert row.cost_usd is None


@pytest.mark.asyncio
async def test_get_usage_history_with_rows_returns_logs():
    """get_usage_history returns the log rows from the query."""
    session = _make_session()
    logs = [MagicMock(), MagicMock(), MagicMock()]

    scalars = MagicMock()
    scalars.all.return_value = logs
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result

    repo = CreditRepository(session)
    history = await repo.get_usage_history(uuid.uuid4(), days=30, limit=50)

    assert history == logs
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_usage_history_with_no_rows_returns_empty_list():
    """get_usage_history returns [] when the user has no recent usage."""
    session = _make_session()
    scalars = MagicMock()
    scalars.all.return_value = []
    result = MagicMock()
    result.scalars.return_value = scalars
    session.execute.return_value = result

    repo = CreditRepository(session)
    history = await repo.get_usage_history(uuid.uuid4())

    assert history == []


@pytest.mark.asyncio
async def test_get_total_used_with_usage_returns_sum():
    """get_total_used returns the summed credits from the query."""
    session = _make_session()
    result = MagicMock()
    result.scalar.return_value = 38
    session.execute.return_value = result

    repo = CreditRepository(session)
    total = await repo.get_total_used(uuid.uuid4(), days=30)

    assert total == 38


@pytest.mark.asyncio
async def test_get_total_used_with_none_scalar_returns_zero():
    """get_total_used returns 0 when the scalar result is None."""
    session = _make_session()
    result = MagicMock()
    result.scalar.return_value = None
    session.execute.return_value = result

    repo = CreditRepository(session)
    total = await repo.get_total_used(uuid.uuid4())

    assert total == 0
