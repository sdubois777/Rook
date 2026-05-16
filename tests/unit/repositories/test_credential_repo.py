"""Tests for CredentialRepository — encrypted token storage and user isolation."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.repositories.credential_repo import CredentialRepository


def _mock_session():
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_yahoo_tokens_stored_encrypted():
    """upsert_yahoo encrypts access_token and refresh_token before storage."""
    session = _mock_session()
    repo = CredentialRepository(session)
    user_id = uuid.uuid4()

    with patch(
        "backend.repositories.credential_repo.encrypt_token"
    ) as mock_encrypt, patch.object(
        repo, "get_for_user", new_callable=AsyncMock, return_value=None
    ):
        mock_encrypt.side_effect = lambda t: f"enc_{t}"

        await repo.upsert_yahoo(
            user_id=user_id,
            access_token="raw_access",
            refresh_token="raw_refresh",
            expires_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    # encrypt_token called for both tokens (once in values, once in set_)
    encrypted_args = [
        call.args[0] for call in mock_encrypt.call_args_list
    ]
    assert "raw_access" in encrypted_args
    assert "raw_refresh" in encrypted_args
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_espn_cookies_stored_encrypted():
    """upsert_espn encrypts espn_s2 and swid before storage."""
    session = _mock_session()
    repo = CredentialRepository(session)
    user_id = uuid.uuid4()

    with patch(
        "backend.repositories.credential_repo.encrypt_token"
    ) as mock_encrypt, patch.object(
        repo, "get_for_user", new_callable=AsyncMock, return_value=None
    ):
        mock_encrypt.side_effect = lambda t: f"enc_{t}"

        await repo.upsert_espn(
            user_id=user_id,
            espn_s2="raw_s2_cookie",
            swid="raw_swid",
        )

    encrypted_args = [
        call.args[0] for call in mock_encrypt.call_args_list
    ]
    assert "raw_s2_cookie" in encrypted_args
    assert "raw_swid" in encrypted_args
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_a_cannot_access_user_b_tokens():
    """get_for_user filters by user_id — different user returns None."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    cred_a = MagicMock()
    cred_a.user_id = user_a
    cred_a.platform = "yahoo"
    cred_a.refresh_token = "enc_refresh"
    cred_a.access_token = "enc_access"
    cred_a.token_expires_at = None

    call_log = []

    async def mock_execute(stmt):
        """Track which user_id is queried via the WHERE clause."""
        result = MagicMock()
        # Extract user_id from the bound params in the statement's whereclause
        clauses = stmt.whereclause
        # The first comparator's right side is the user_id param
        user_id_param = clauses.clauses[0].right.value
        call_log.append(user_id_param)

        if user_id_param == user_a:
            result.scalar_one_or_none.return_value = cred_a
        else:
            result.scalar_one_or_none.return_value = None
        return result

    session = _mock_session()
    session.execute = mock_execute
    repo = CredentialRepository(session)

    # User A sees their own credentials
    result_a = await repo.get_for_user(user_a, "yahoo")
    assert result_a is not None
    assert result_a.user_id == user_a

    # User B cannot see User A's credentials
    result_b = await repo.get_for_user(user_b, "yahoo")
    assert result_b is None

    # Both queries were issued with correct user_ids
    assert call_log == [user_a, user_b]
