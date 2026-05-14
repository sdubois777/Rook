"""Tests for UserService — account management."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.exceptions import ValidationError
from backend.services.user_service import UserService


def _make_repo():
    repo = AsyncMock()
    return repo


def _make_user(tier="intro", credits=0, external_id="ext-001"):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.external_id = external_id
    user.email = f"{external_id}@test.com"
    user.tier = tier
    user.credits_remaining = credits
    return user


@pytest.mark.asyncio
async def test_get_or_create_creates_new_user():
    repo = _make_repo()
    repo.get_by_external_id.return_value = None
    new_user = _make_user()
    repo.create.return_value = new_user

    service = UserService(repo)
    user, created = await service.get_or_create(
        external_id="ext-new", email="new@test.com"
    )

    assert created is True
    repo.create.assert_awaited_once()
    repo.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_returns_existing():
    repo = _make_repo()
    existing = _make_user()
    repo.get_by_external_id.return_value = existing

    service = UserService(repo)
    user, created = await service.get_or_create(
        external_id="ext-001", email="test@test.com"
    )

    assert created is False
    assert user is existing
    repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_user_starts_on_intro_tier():
    repo = _make_repo()
    repo.get_by_external_id.return_value = None
    new_user = _make_user(tier="intro")
    repo.create.return_value = new_user

    service = UserService(repo)
    user, _ = await service.get_or_create(
        external_id="ext-new", email="new@test.com"
    )

    # Verify create was called with tier="intro"
    call_kwargs = repo.create.call_args[1]
    assert call_kwargs["tier"] == "intro"


@pytest.mark.asyncio
async def test_new_intro_user_gets_signup_bonus():
    repo = _make_repo()
    repo.get_by_external_id.return_value = None
    new_user = _make_user(credits=25)
    repo.create.return_value = new_user

    service = UserService(repo)
    user, created = await service.get_or_create(
        external_id="ext-new", email="new@test.com"
    )

    assert created is True
    call_kwargs = repo.create.call_args[1]
    assert call_kwargs["credits_remaining"] == 25
    assert call_kwargs["tier"] == "intro"


@pytest.mark.asyncio
async def test_existing_user_credits_unchanged_on_get():
    repo = _make_repo()
    existing = _make_user(credits=25)
    repo.get_by_external_id.return_value = existing

    service = UserService(repo)
    user, created = await service.get_or_create(
        external_id="ext-001", email="test@test.com"
    )

    assert created is False
    assert user.credits_remaining == 25
    repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_signup_bonus_applied_correctly():
    repo = _make_repo()
    user = _make_user(tier="standard", credits=0)
    repo.get_or_404.return_value = user

    service = UserService(repo)
    await service.apply_signup_bonus(user)

    # Standard tier signup bonus = 75
    repo.update_credits.assert_awaited_once_with(user.id, delta=75)
    repo.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_signup_bonus_intro_tier():
    repo = _make_repo()
    user = _make_user(tier="intro", credits=0)
    repo.get_or_404.return_value = user

    service = UserService(repo)
    await service.apply_signup_bonus(user)

    # Intro tier signup bonus = 25
    repo.update_credits.assert_awaited_once_with(user.id, delta=25)


@pytest.mark.asyncio
async def test_upgrade_tier_invalid_raises():
    repo = _make_repo()
    user = _make_user(tier="intro")

    service = UserService(repo)
    with pytest.raises(ValidationError):
        await service.upgrade_tier(user, "platinum")


@pytest.mark.asyncio
async def test_upgrade_tier_valid():
    repo = _make_repo()
    user = _make_user(tier="intro")
    upgraded = _make_user(tier="standard", credits=75)
    repo.update_tier.return_value = upgraded

    service = UserService(repo)
    result = await service.upgrade_tier(user, "standard")

    repo.update_tier.assert_awaited_once_with(
        user.id, tier="standard", credits_bonus=75
    )
    repo.commit.assert_awaited_once()
