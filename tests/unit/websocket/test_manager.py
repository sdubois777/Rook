"""Tests for backend/websocket/manager.py"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.websocket.manager import WebSocketManager


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
