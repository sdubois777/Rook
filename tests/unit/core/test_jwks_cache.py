"""F12 — a bad token must NOT invalidate the shared JWKS cache.

Verifies: an invalid/expired token leaves _jwks_cache intact; a stream of invalid
tokens triggers at most the initial fetch (not one per token); a valid token after
an invalid one needs no extra fetch; and genuine key rotation (kid not in the
cached JWKS) triggers exactly one refetch-and-retry.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from jose import JWTError

import backend.core.dependencies as deps
from backend.core.exceptions import UnauthorizedError


@pytest.fixture(autouse=True)
def _reset_jwks_cache():
    """The JWKS cache is a module global — reset it around each test so runs are
    independent of order (and of the TTL warming from a prior test)."""
    deps._jwks_cache = None
    deps._jwks_fetched_at = 0.0
    yield
    deps._jwks_cache = None
    deps._jwks_fetched_at = 0.0


def _jwks(*kids):
    return {"keys": [{"kid": k} for k in kids]}


@pytest.mark.asyncio
async def test_invalid_token_does_not_null_cache():
    """A routine bad token raises 401 but leaves the JWKS cache populated."""
    fetch = AsyncMock(return_value=_jwks("k1"))
    with patch.object(deps, "_get_clerk_jwks", fetch), patch.object(
        deps, "_decode_clerk", side_effect=JWTError("bad signature")
    ):
        with pytest.raises(UnauthorizedError):
            await deps._verify_clerk_jwt("bad.token.value")

    assert deps._jwks_cache == _jwks("k1")   # cache intact, NOT None
    assert fetch.call_count == 1             # only the initial populate


@pytest.mark.asyncio
async def test_many_invalid_tokens_trigger_bounded_fetches():
    """N invalid tokens must NOT drive N outbound Clerk fetches — the cache is
    populated once and reused (no re-null, no amplification)."""
    fetch = AsyncMock(return_value=_jwks("k1"))
    with patch.object(deps, "_get_clerk_jwks", fetch), patch.object(
        deps, "_decode_clerk", side_effect=JWTError("bad")
    ):
        for _ in range(5):
            with pytest.raises(UnauthorizedError):
                await deps._verify_clerk_jwt("bad.token.value")

    assert fetch.call_count == 1             # 5 bad tokens → still just 1 fetch


@pytest.mark.asyncio
async def test_valid_token_after_invalid_needs_no_extra_fetch():
    """A valid token following an invalid one verifies from the warm cache."""
    fetch = AsyncMock(return_value=_jwks("k1"))
    decode = patch.object(
        deps, "_decode_clerk", side_effect=[JWTError("bad"), {"sub": "user_1"}]
    )
    with patch.object(deps, "_get_clerk_jwks", fetch), decode:
        with pytest.raises(UnauthorizedError):
            await deps._verify_clerk_jwt("bad.token.value")
        payload = await deps._verify_clerk_jwt("good.token.value")

    assert payload["sub"] == "user_1"
    assert fetch.call_count == 1             # no extra fetch for the valid token


@pytest.mark.asyncio
async def test_key_rotation_triggers_exactly_one_refetch():
    """A token whose kid is absent from the cached JWKS (genuine rotation) forces
    exactly one refetch, then verifies against the fresh keys."""
    # Initial populate returns the OLD keyset; the rotation refetch returns NEW.
    fetch = AsyncMock(side_effect=[_jwks("kold"), _jwks("knew")])
    # First decode (old keys) fails with a kid-miss; second (new keys) succeeds.
    decode = patch.object(
        deps, "_decode_clerk", side_effect=[JWTError("kid"), {"sub": "user_1"}]
    )
    header = patch.object(
        deps.jwt, "get_unverified_header", return_value={"kid": "knew"}
    )
    with patch.object(deps, "_get_clerk_jwks", fetch), decode, header:
        payload = await deps._verify_clerk_jwt("rotated.token.value")

    assert payload["sub"] == "user_1"
    assert fetch.call_count == 2             # initial populate + exactly one refetch
    assert deps._jwks_cache == _jwks("knew")  # cache now holds the rotated keys


@pytest.mark.asyncio
async def test_malformed_token_is_not_treated_as_rotation():
    """A garbage token whose header can't be parsed is a client error, NOT a
    kid-miss — it must not trigger a refetch."""
    fetch = AsyncMock(return_value=_jwks("k1"))
    # Real get_unverified_header on garbage raises → _token_kid_missing returns False.
    with patch.object(deps, "_get_clerk_jwks", fetch), patch.object(
        deps, "_decode_clerk", side_effect=JWTError("malformed")
    ):
        with pytest.raises(UnauthorizedError):
            await deps._verify_clerk_jwt("not-a-jwt")

    assert fetch.call_count == 1             # no rotation refetch
