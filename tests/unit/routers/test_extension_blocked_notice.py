"""A refused draft event must TELL the user's room why (#461).

Before this, refusing an event returned 403 and nothing else. The extension
discards the response status (postDraftEvent in extension/src/utils/api.js), so
the refusal was invisible everywhere: the extension popup still read "Connected"
and still read "relaying", and the draft room simply never updated. A customer
reported that state as "not synced to the draft on ESPN", and it could not be
told apart from a working draft without reading production logs.

The server knows both WHO it refused and WHY at the moment it refuses, so it now
pushes that to the user's own WebSocket clients.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.routers import draft as draft_mod


def _free_user():
    """A user whose effective plan does not include live draft."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tier = "free"
    user.tier_expires_at = None
    return user


def _paid_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.tier = "standard"
    user.tier_expires_at = None
    return user


def _patch_user(user):
    repo = MagicMock()
    repo.get_by_draft_token = AsyncMock(return_value=user)
    return patch("backend.repositories.user_repo.UserRepository", return_value=repo)


def _event():
    return draft_mod.DraftEventPayload(
        type="snake_status",
        platform="espn",
        payload={"current_pick": 11, "current_round": 1},
    )


@pytest.fixture(autouse=True)
def _clear_throttle():
    """The throttle is module state; a leaked entry would silence another test."""
    draft_mod._last_blocked_notice.clear()
    yield
    draft_mod._last_blocked_notice.clear()


async def test_refused_event_tells_that_users_room_why():
    user = _free_user()
    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock()

    with _patch_user(user), patch.object(draft_mod, "ws_manager", ws):
        resp = await draft_mod.relay_draft_event(
            _event(), x_draft_token="tok", db=MagicMock()
        )

    assert resp.status_code == 403

    ws.broadcast_to_session.assert_awaited_once()
    session_key, message = ws.broadcast_to_session.await_args[0]
    # ONLY this user's clients — never a broadcast to everyone.
    assert session_key == str(user.id)
    assert message["type"] == "extension_blocked"
    assert message["payload"]["code"] == "live_draft_requires_paid_plan"
    # A human-readable reason, not just a code: this is rendered verbatim.
    assert "plan" in message["payload"]["message"].lower()


async def test_the_notice_is_throttled_not_sent_once_per_event():
    """A refused extension keeps posting about once a second for a whole draft.
    Without a throttle one blocked draft would put thousands of identical
    messages on that user's socket."""
    user = _free_user()
    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock()

    with _patch_user(user), patch.object(draft_mod, "ws_manager", ws):
        for _ in range(25):
            resp = await draft_mod.relay_draft_event(
                _event(), x_draft_token="tok", db=MagicMock()
            )
            assert resp.status_code == 403

    assert ws.broadcast_to_session.await_count == 1


async def test_two_refused_users_each_get_their_own_notice():
    """The throttle is per user — one blocked draft must not silence another."""
    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock()
    keys = []
    for _ in range(2):
        user = _free_user()
        keys.append(str(user.id))
        with _patch_user(user), patch.object(draft_mod, "ws_manager", ws):
            await draft_mod.relay_draft_event(
                _event(), x_draft_token="tok", db=MagicMock()
            )

    assert ws.broadcast_to_session.await_count == 2
    notified = [c[0][0] for c in ws.broadcast_to_session.await_args_list]
    assert sorted(notified) == sorted(keys)


async def test_a_failed_notice_never_changes_the_response():
    """Best-effort: telling the room must not turn a refusal into a 500."""
    user = _free_user()
    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock(side_effect=RuntimeError("socket gone"))

    with _patch_user(user), patch.object(draft_mod, "ws_manager", ws):
        resp = await draft_mod.relay_draft_event(
            _event(), x_draft_token="tok", db=MagicMock()
        )

    assert resp.status_code == 403


async def test_an_accepted_event_sends_no_notice():
    """The banner must never appear for a draft that is working."""
    user = _paid_user()
    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock()
    sm = MagicMock()
    sm.get_or_rehydrate = AsyncMock(return_value=None)
    sm.persist = AsyncMock()

    with _patch_user(user), patch.object(draft_mod, "ws_manager", ws), \
            patch.object(draft_mod, "session_manager", sm):
        await draft_mod.relay_draft_event(
            _event(), x_draft_token="tok", db=MagicMock()
        )

    sent_types = [
        c[0][1].get("type") for c in ws.broadcast_to_session.await_args_list
    ]
    assert "extension_blocked" not in sent_types
    # The draft event itself still reaches the room.
    assert "snake_status" in sent_types
