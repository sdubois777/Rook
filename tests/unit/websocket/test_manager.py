"""Tests for backend/websocket/manager.py"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.websocket.manager import SessionScopedBroadcaster, WebSocketManager


def _make_ws():
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_connect_accepts_and_registers():
    """connect() accepts the socket and tracks it."""
    manager = WebSocketManager()
    ws = _make_ws()

    await manager.connect(ws)

    ws.accept.assert_awaited_once()
    assert manager.connection_count == 1


@pytest.mark.asyncio
async def test_disconnect_removes_connection():
    """disconnect() removes the socket; double disconnect is harmless."""
    manager = WebSocketManager()
    ws = _make_ws()
    await manager.connect(ws)

    manager.disconnect(ws)
    assert manager.connection_count == 0

    manager.disconnect(ws)  # already gone — must not raise
    assert manager.connection_count == 0


@pytest.mark.asyncio
async def test_broadcast_sends_to_all_clients():
    """broadcast() pushes the message to every connected socket."""
    manager = WebSocketManager()
    ws1, ws2 = _make_ws(), _make_ws()
    await manager.connect(ws1)
    await manager.connect(ws2)

    await manager.broadcast({"event": "pick"})

    ws1.send_json.assert_awaited_once_with({"event": "pick"})
    ws2.send_json.assert_awaited_once_with({"event": "pick"})


@pytest.mark.asyncio
async def test_broadcast_removes_dead_connection_and_continues():
    """A failing socket is dropped; healthy sockets still receive."""
    manager = WebSocketManager()
    dead, alive = _make_ws(), _make_ws()
    dead.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    await manager.connect(dead)
    await manager.connect(alive)

    await manager.broadcast({"event": "bid"})

    alive.send_json.assert_awaited_once_with({"event": "bid"})
    assert manager.connection_count == 1
    assert dead not in manager.active_connections


# --- session-keyed isolation (per-user draft routing) ---


@pytest.mark.asyncio
async def test_broadcast_to_session_only_reaches_that_session():
    """A draft event for user A must NOT reach user B's connection."""
    manager = WebSocketManager()
    ws_a, ws_b = _make_ws(), _make_ws()
    await manager.connect(ws_a, session_key="user-a")
    await manager.connect(ws_b, session_key="user-b")

    await manager.broadcast_to_session("user-a", {"event": "nomination"})

    ws_a.send_json.assert_awaited_once_with({"event": "nomination"})
    ws_b.send_json.assert_not_awaited()  # no cross-broadcast


@pytest.mark.asyncio
async def test_broadcast_all_still_reaches_every_session():
    """broadcast() (news) fans out across session buckets."""
    manager = WebSocketManager()
    ws_a, ws_b = _make_ws(), _make_ws()
    await manager.connect(ws_a, session_key="user-a")
    await manager.connect(ws_b, session_key="user-b")

    await manager.broadcast({"event": "news"})

    ws_a.send_json.assert_awaited_once()
    ws_b.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_scoped_broadcaster_routes_to_its_session():
    """The adapter handed to an engine routes .broadcast() to one session."""
    manager = WebSocketManager()
    ws_a, ws_b = _make_ws(), _make_ws()
    await manager.connect(ws_a, session_key="user-a")
    await manager.connect(ws_b, session_key="user-b")

    broadcaster = SessionScopedBroadcaster(manager, "user-a")
    await broadcaster.broadcast({"type": "recommendation"})

    ws_a.send_json.assert_awaited_once_with({"type": "recommendation"})
    ws_b.send_json.assert_not_awaited()
