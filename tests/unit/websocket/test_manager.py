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
    manager = WebSocketManager(channel="test")
    ws = _make_ws()

    await manager.connect(ws)

    ws.accept.assert_awaited_once()
    assert manager.connection_count == 1


@pytest.mark.asyncio
async def test_disconnect_removes_connection():
    """disconnect() removes the socket; double disconnect is harmless."""
    manager = WebSocketManager(channel="test")
    ws = _make_ws()
    await manager.connect(ws)

    manager.disconnect(ws)
    assert manager.connection_count == 0

    manager.disconnect(ws)  # already gone — must not raise
    assert manager.connection_count == 0


@pytest.mark.asyncio
async def test_broadcast_sends_to_all_clients():
    """broadcast() pushes the message to every connected socket."""
    manager = WebSocketManager(channel="test")
    ws1, ws2 = _make_ws(), _make_ws()
    await manager.connect(ws1)
    await manager.connect(ws2)

    await manager.broadcast({"event": "pick"})

    ws1.send_json.assert_awaited_once_with({"event": "pick"})
    ws2.send_json.assert_awaited_once_with({"event": "pick"})


@pytest.mark.asyncio
async def test_broadcast_removes_dead_connection_and_continues():
    """A failing socket is dropped; healthy sockets still receive."""
    manager = WebSocketManager(channel="test")
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
    manager = WebSocketManager(channel="test")
    ws_a, ws_b = _make_ws(), _make_ws()
    await manager.connect(ws_a, session_key="user-a")
    await manager.connect(ws_b, session_key="user-b")

    await manager.broadcast_to_session("user-a", {"event": "nomination"})

    ws_a.send_json.assert_awaited_once_with({"event": "nomination"})
    ws_b.send_json.assert_not_awaited()  # no cross-broadcast


@pytest.mark.asyncio
async def test_broadcast_all_still_reaches_every_session():
    """broadcast() (news) fans out across session buckets."""
    manager = WebSocketManager(channel="test")
    ws_a, ws_b = _make_ws(), _make_ws()
    await manager.connect(ws_a, session_key="user-a")
    await manager.connect(ws_b, session_key="user-b")

    await manager.broadcast({"event": "news"})

    ws_a.send_json.assert_awaited_once()
    ws_b.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_scoped_broadcaster_routes_to_its_session():
    """The adapter handed to an engine routes .broadcast() to one session."""
    manager = WebSocketManager(channel="test")
    ws_a, ws_b = _make_ws(), _make_ws()
    await manager.connect(ws_a, session_key="user-a")
    await manager.connect(ws_b, session_key="user-b")

    broadcaster = SessionScopedBroadcaster(manager, "user-a")
    await broadcaster.broadcast({"type": "recommendation"})

    ws_a.send_json.assert_awaited_once_with({"type": "recommendation"})
    ws_b.send_json.assert_not_awaited()


# --- cross-process pub/sub (Postgres LISTEN/NOTIFY seam) ---
#
# A fake in-memory bus stands in for Postgres: publish() records the payload and
# fans it to every manager subscribed to the channel — exactly what NOTIFY→LISTEN
# does across processes, without a DB.


class _FakeBus:
    def __init__(self):
        self._subs = {}      # channel -> list[handler]
        self.published = []  # (channel, payload)
        self.running = True

    @property
    def is_running(self):
        return self.running

    def subscribe(self, channel, handler):
        self._subs.setdefault(channel, []).append(handler)

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        for handler in self._subs.get(channel, []):
            await handler(payload)   # deliver to every subscribed process


@pytest.mark.asyncio
async def test_broadcast_publishes_to_bus_and_delivers_locally():
    """With a bus, a broadcast delivers locally AND publishes for other processes."""
    bus = _FakeBus()
    mgr = WebSocketManager(channel="ws_draft", bus=bus)
    ws = _make_ws()
    await mgr.connect(ws, session_key="user-a")

    await mgr.broadcast_to_session("user-a", {"type": "recommendation"})

    ws.send_json.assert_awaited_once_with({"type": "recommendation"})  # local delivery
    assert len(bus.published) == 1                                      # published for peers
    assert bus.published[0][0] == "ws_draft"


@pytest.mark.asyncio
async def test_cross_process_delivery_two_managers_one_bus():
    """THE POINT: a message published on manager A reaches a socket held only by
    manager B (a different 'process') via the shared bus."""
    bus = _FakeBus()
    proc_a = WebSocketManager(channel="ws_draft", bus=bus)
    proc_b = WebSocketManager(channel="ws_draft", bus=bus)
    ws_on_b = _make_ws()
    await proc_b.connect(ws_on_b, session_key="user-x")   # socket lives on B

    # A produces the rec (A holds no socket for user-x)
    await proc_a.broadcast_to_session("user-x", {"type": "recommendation", "player": "Gibbs"})

    ws_on_b.send_json.assert_awaited_once_with(
        {"type": "recommendation", "player": "Gibbs"}
    )


@pytest.mark.asyncio
async def test_origin_skip_prevents_double_delivery():
    """The publishing process ignores its own notification (already delivered
    locally) — no double send."""
    bus = _FakeBus()
    mgr = WebSocketManager(channel="ws_draft", bus=bus)
    ws = _make_ws()
    await mgr.connect(ws, session_key="user-a")

    await mgr.broadcast_to_session("user-a", {"n": 1})

    # local (1) + its own notification came back through the bus and was skipped
    ws.send_json.assert_awaited_once_with({"n": 1})


@pytest.mark.asyncio
async def test_publish_failure_never_breaks_local_delivery():
    """If the bus publish raises (e.g. LISTEN conn mid-reconnect), local delivery
    is unaffected — single-process must never regress."""
    bus = _FakeBus()

    async def boom(channel, payload):
        raise RuntimeError("bus down")

    bus.publish = boom
    mgr = WebSocketManager(channel="ws_draft", bus=bus)
    ws = _make_ws()
    await mgr.connect(ws, session_key="user-a")

    await mgr.broadcast_to_session("user-a", {"ok": True})  # must not raise

    ws.send_json.assert_awaited_once_with({"ok": True})
