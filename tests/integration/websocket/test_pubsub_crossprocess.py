"""Cross-process WebSocket fan-out — REAL Postgres LISTEN/NOTIFY round-trip.

This is the proof that the horizontal-scaling seam works: a message published on
one PubSub/manager pair (standing in for process A) is delivered to a socket held
ONLY by a second, independent PubSub/manager pair (process B) — the two do not
share Python state, only the Postgres channel. Each PostgresPubSub opens its OWN
dedicated LISTEN connection, exactly as two OS processes would.

DB-gated: skips cleanly when Postgres is unreachable (e.g. CI without a database),
so it never turns the suite red on a box that has no DB.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.config import settings
from backend.websocket.manager import WebSocketManager
from backend.websocket.pubsub import PostgresPubSub, asyncpg_dsn

asyncpg = pytest.importorskip("asyncpg")

_DSN = asyncpg_dsn(settings.database_url)
# Unique per test module run so we never collide with the live ws_draft/ws_news
# channels on a shared database.
_CHANNEL = "ws_test_xproc"


async def _db_reachable() -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(_DSN), timeout=5)
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture(scope="module", autouse=True)
async def _require_db():
    if not await _db_reachable():
        pytest.skip("Postgres not reachable — skipping cross-process pubsub tests")


def _make_ws():
    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


async def _notify_via_pool(channel: str, payload: str) -> None:
    """Publisher executor for the test: a fresh short-lived asyncpg connection
    issues the NOTIFY (mirrors the app's 'NOTIFY through the request pool')."""
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("SELECT pg_notify($1, $2)", channel, payload)
    finally:
        await conn.close()


async def _wait_for(predicate, timeout=8.0, interval=0.1):
    """Poll until predicate() is truthy or timeout — delivery is async (NOTIFY
    round-trips through Postgres, then _dispatch schedules the handler)."""
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return False


@pytest.mark.asyncio
async def test_cross_process_delivery_real_postgres():
    """THE POINT: publish on 'process A', socket lives on 'process B', message
    crosses via a real Postgres NOTIFY -> LISTEN round-trip."""
    bus_a = PostgresPubSub(_DSN, publish_exec=_notify_via_pool)
    bus_b = PostgresPubSub(_DSN, publish_exec=_notify_via_pool)
    proc_a = WebSocketManager(channel=_CHANNEL, bus=bus_a)
    proc_b = WebSocketManager(channel=_CHANNEL, bus=bus_b)
    ws_on_b = _make_ws()
    await proc_b.connect(ws_on_b, session_key="user-x")

    await bus_a.start()
    await bus_b.start()
    try:
        # give both LISTEN loops a moment to establish add_listener
        assert await _wait_for(lambda: bus_a.is_running and bus_b.is_running)
        await asyncio.sleep(0.5)

        # A produces the rec; A holds no socket for user-x
        await proc_a.broadcast_to_session(
            "user-x", {"type": "recommendation", "player": "Gibbs"}
        )

        assert await _wait_for(lambda: ws_on_b.send_json.await_count == 1), (
            "message published on A never reached the socket held by B"
        )
        ws_on_b.send_json.assert_awaited_once_with(
            {"type": "recommendation", "player": "Gibbs"}
        )
    finally:
        await bus_a.stop()
        await bus_b.stop()


@pytest.mark.asyncio
async def test_both_directions_and_concurrent_sessions():
    """Both directions (A->B and B->A) plus multiple concurrent sessions split
    across the two processes."""
    bus_a = PostgresPubSub(_DSN, publish_exec=_notify_via_pool)
    bus_b = PostgresPubSub(_DSN, publish_exec=_notify_via_pool)
    proc_a = WebSocketManager(channel=_CHANNEL, bus=bus_a)
    proc_b = WebSocketManager(channel=_CHANNEL, bus=bus_b)

    ws_a = _make_ws()   # session s1 lives on A
    ws_b = _make_ws()   # session s2 lives on B
    await proc_a.connect(ws_a, session_key="s1")
    await proc_b.connect(ws_b, session_key="s2")

    await bus_a.start()
    await bus_b.start()
    try:
        await asyncio.sleep(0.5)

        # B produces a rec for s1 (socket on A)  -> B->A direction
        await proc_b.broadcast_to_session("s1", {"n": "for-s1"})
        # A produces a rec for s2 (socket on B)  -> A->B direction
        await proc_a.broadcast_to_session("s2", {"n": "for-s2"})

        assert await _wait_for(
            lambda: ws_a.send_json.await_count == 1 and ws_b.send_json.await_count == 1
        ), "cross-process delivery failed in one or both directions"

        ws_a.send_json.assert_awaited_once_with({"n": "for-s1"})
        ws_b.send_json.assert_awaited_once_with({"n": "for-s2"})
        # isolation held: s1's socket did NOT get s2's message and vice versa
        assert ws_a.send_json.await_count == 1
        assert ws_b.send_json.await_count == 1
    finally:
        await bus_a.stop()
        await bus_b.stop()


@pytest.mark.asyncio
async def test_listen_connection_reconnects_after_drop():
    """Railway drops idle connections — kill the LISTEN connection out from under
    the loop and confirm it re-establishes and resumes delivery."""
    bus_a = PostgresPubSub(_DSN, publish_exec=_notify_via_pool)
    bus_b = PostgresPubSub(_DSN, publish_exec=_notify_via_pool)
    proc_a = WebSocketManager(channel=_CHANNEL, bus=bus_a)
    proc_b = WebSocketManager(channel=_CHANNEL, bus=bus_b)
    ws_on_b = _make_ws()
    await proc_b.connect(ws_on_b, session_key="user-r")

    await bus_a.start()
    await bus_b.start()
    try:
        await asyncio.sleep(0.5)

        # Kill B's LISTEN connection from the server side (simulates Railway drop).
        killer = await asyncpg.connect(_DSN)
        try:
            b_pid = bus_b._conn.get_server_pid()  # noqa: SLF001 — test needs the pid
            await killer.execute(
                "SELECT pg_terminate_backend($1)", b_pid
            )
        finally:
            await killer.close()

        # The termination listener fires on the drop and the loop reconnects.
        # Wait until B has a fresh, live connection (different pid). is_closed()
        # and get_server_pid() are sync — keep the predicate sync.
        def _reconnected():
            c = bus_b._conn  # noqa: SLF001
            return c is not None and not c.is_closed() and c.get_server_pid() != b_pid

        assert await _wait_for(_reconnected, timeout=45.0), (
            "LISTEN loop did not re-establish after the connection was killed"
        )
        await asyncio.sleep(0.5)  # let add_listener re-register on the new conn

        # Delivery resumes on the reconnected LISTEN connection.
        await proc_a.broadcast_to_session("user-r", {"resumed": True})
        assert await _wait_for(lambda: ws_on_b.send_json.await_count == 1), (
            "delivery did not resume after LISTEN reconnect"
        )
        ws_on_b.send_json.assert_awaited_once_with({"resumed": True})
    finally:
        await bus_a.stop()
        await bus_b.stop()
