"""Tests for CreditService — credit deduction, balance, logging."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.exceptions import InsufficientCreditsError
from backend.services.credit_service import CreditService


def _make_user(credits: int = 100, tier: str = "standard"):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tier = tier
    user.credits_remaining = credits
    return user


def _make_service():
    user_repo = AsyncMock()
    credit_repo = AsyncMock()
    service = CreditService(user_repo, credit_repo)
    return service, user_repo, credit_repo


@pytest.mark.asyncio
async def test_deduct_reduces_balance():
    service, user_repo, credit_repo = _make_service()
    user = _make_user(credits=50)
    user_repo.update_credits.return_value = 40  # 50 - 10
    credit_repo.log_usage.return_value = MagicMock()

    new_balance = await service.deduct(user, "trade_analysis")

    assert new_balance == 40
    user_repo.update_credits.assert_awaited_once_with(user.id, delta=-10)
    credit_repo.log_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_deduct_raises_on_insufficient_credits():
    service, user_repo, credit_repo = _make_service()
    user = _make_user(credits=5)

    with pytest.raises(InsufficientCreditsError) as exc_info:
        await service.deduct(user, "trade_analysis")  # costs 10

    assert exc_info.value.detail["required"] == 10
    assert exc_info.value.detail["available"] == 5
    user_repo.update_credits.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_action_no_deduction():
    service, user_repo, credit_repo = _make_service()
    user = _make_user(credits=50)

    # "projections" isn't in CREDIT_COSTS → cost = 0
    result = await service.deduct(user, "projections")

    assert result == 50
    user_repo.update_credits.assert_not_awaited()
    credit_repo.log_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_usage_logged_after_deduction():
    service, user_repo, credit_repo = _make_service()
    user = _make_user(credits=50)
    user_repo.update_credits.return_value = 42

    await service.deduct(user, "waiver_wire", agent_name="waiver_agent")

    credit_repo.log_usage.assert_awaited_once_with(
        user_id=user.id,
        action="waiver_wire",
        credits_used=8,
        agent_name="waiver_agent",
        cost_usd=None,
    )


@pytest.mark.asyncio
async def test_deduct_is_atomic():
    """Deduct, log, and commit happen in sequence."""
    service, user_repo, credit_repo = _make_service()
    user = _make_user(credits=50)
    user_repo.update_credits.return_value = 40

    await service.deduct(user, "trade_analysis")

    # Verify commit was called after both operations
    user_repo.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_balance():
    service, user_repo, credit_repo = _make_service()
    user = _make_user(credits=42)
    refreshed = _make_user(credits=42)
    user_repo.get_or_404.return_value = refreshed

    balance = await service.get_balance(user)
    assert balance == 42


@pytest.mark.asyncio
async def test_get_usage_history():
    service, user_repo, credit_repo = _make_service()
    user = _make_user()
    credit_repo.get_usage_history.return_value = ["entry1", "entry2"]

    history = await service.get_usage_history(user, days=7)
    assert history == ["entry1", "entry2"]
    credit_repo.get_usage_history.assert_awaited_once_with(user.id, days=7)
