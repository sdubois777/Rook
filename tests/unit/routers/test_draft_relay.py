"""relay_draft_event must ALWAYS broadcast the raw draft event to the user's
WebSocket session, even if the per-event engine processing (DB lookup, Sonnet
recommendation, state persist) throws.

Why: the room is driven entirely by these raw events — nominee on the block,
timer, bids, picks leaving the list, opponent budgets. A swallowed broadcast
(candidate-B failure) would freeze the room on any engine hiccup. Covers BOTH
draft types so neither auction nor snake can silently lose event delivery.

(The auction outage in production was a SEPARATE cause — the extension's
`#draft` DOM poller stopped emitting, so nothing reached this relay at all —
but this hardening closes the latent backend path regardless.)
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.routers import draft as draft_mod


def _fake_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    return user


def _patch_user(user):
    # UserRepository is imported INSIDE relay_draft_event — patch it at its source.
    repo = MagicMock()
    repo.get_by_draft_token = AsyncMock(return_value=user)
    return patch("backend.repositories.user_repo.UserRepository", return_value=repo)


def _fake_session_manager():
    session = MagicMock()
    session.engine = MagicMock()
    session.state = MagicMock()
    sm = MagicMock()
    sm.get_or_rehydrate = AsyncMock(return_value=session)
    sm.create = AsyncMock(return_value=session)
    sm.persist = AsyncMock()
    return sm


def _fake_ws():
    ws = MagicMock()
    ws.broadcast_to_session = AsyncMock()
    return ws


# event.type -> the engine helper that runs for it (patched to raise / no-op)
_ENGINE_HELPER = {
    "nomination": "_trigger_nomination",   # auction
    "draft_pick": "_record_pick",          # auction
    "your_turn": "_trigger_your_turn",     # snake
}


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["nomination", "draft_pick", "your_turn"])
async def test_raw_event_relayed_even_when_engine_throws(event_type):
    """A throw in engine processing must NOT suppress the raw broadcast."""
    user = _fake_user()
    sm = _fake_session_manager()
    ws = _fake_ws()
    payload = {"player_name": "Saquon Barkley", "final_price": 41, "winner": "T1"}
    event = draft_mod.DraftEventPayload(type=event_type, platform="yahoo", payload=payload)

    with _patch_user(user), \
            patch.object(draft_mod, "session_manager", sm), \
            patch.object(draft_mod, "ws_manager", ws), \
            patch.object(
                draft_mod, _ENGINE_HELPER[event_type],
                new=AsyncMock(side_effect=RuntimeError("engine boom")),
            ):
        result = await draft_mod.relay_draft_event(event, x_draft_token="tok", db=AsyncMock())

    assert result == {"status": "relayed"}
    ws.broadcast_to_session.assert_awaited_once()
    session_key, message = ws.broadcast_to_session.await_args.args
    assert session_key == str(user.id)        # the user's own session
    assert message["type"] == event_type      # raw event delivered despite the throw
    assert message["payload"] == payload


@pytest.mark.asyncio
async def test_happy_path_still_relays():
    """Normal processing also broadcasts the raw event (no regression)."""
    user = _fake_user()
    sm = _fake_session_manager()
    ws = _fake_ws()
    event = draft_mod.DraftEventPayload(
        type="nomination", platform="yahoo", payload={"player_name": "Bijan Robinson"}
    )

    with _patch_user(user), \
            patch.object(draft_mod, "session_manager", sm), \
            patch.object(draft_mod, "ws_manager", ws), \
            patch.object(draft_mod, "_trigger_nomination", new=AsyncMock()):
        result = await draft_mod.relay_draft_event(event, x_draft_token="tok", db=AsyncMock())

    assert result == {"status": "relayed"}
    ws.broadcast_to_session.assert_awaited_once()
    session_key, message = ws.broadcast_to_session.await_args.args
    assert session_key == str(user.id)
    assert message["type"] == "nomination"


@pytest.mark.asyncio
async def test_invalid_token_rejected_and_nothing_broadcast():
    """An invalid draft token 401s before any broadcast."""
    repo = MagicMock()
    repo.get_by_draft_token = AsyncMock(return_value=None)
    ws = _fake_ws()
    event = draft_mod.DraftEventPayload(type="clock", platform="yahoo", payload={})

    with patch("backend.repositories.user_repo.UserRepository", return_value=repo), \
            patch.object(draft_mod, "ws_manager", ws):
        with pytest.raises(draft_mod.HTTPException) as exc:
            await draft_mod.relay_draft_event(event, x_draft_token="bad", db=AsyncMock())

    assert exc.value.status_code == 401
    ws.broadcast_to_session.assert_not_awaited()
