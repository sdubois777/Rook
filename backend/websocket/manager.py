"""
backend/websocket/manager.py

WebSocket connection manager — push-based, never polls.

Connections are grouped by a session key so draft events reach ONLY the clients
watching that draft (per-user isolation): `broadcast_to_session(key, msg)`.
`broadcast(msg)` still fans out to every connection — used by the news manager,
where all clients should receive every signal.

CROSS-PROCESS (the horizontal-scaling unlock): `_sessions` is per-process — it only
knows the sockets THIS process holds. Delivery is now LOCAL-FIRST + PUBLISH: every
broadcast writes to this process's sockets immediately (unchanged, reliable, needs
no DB), then best-effort publishes to a Postgres LISTEN/NOTIFY bus so OTHER
processes deliver to the sockets THEY hold. Each process ignores its own
publications (origin skip), so there's no double-delivery. If the bus is absent or
not running (tests, or a transient LISTEN drop), local delivery is unaffected —
single-process behavior is byte-identical to before.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Optional

from fastapi import WebSocket

from backend.websocket.pubsub import NOTIFY_MAX_BYTES, PubSubBackend

logger = logging.getLogger(__name__)

# Default bucket for sessionless connections (news feed, or any caller that does
# not pass a key). Draft connections pass the user's session key instead.
GLOBAL_SESSION = "__global__"


class WebSocketManager:
    """
    Manages active WebSocket connections, grouped by session key.

    Thread-safety note: FastAPI runs async. All mutations happen inside async
    context on the same event loop — no explicit locks needed.
    """

    def __init__(
        self, *, channel: str, bus: Optional[PubSubBackend] = None
    ) -> None:
        # session_key -> list of connections for that session
        self._sessions: dict[str, list[WebSocket]] = {}
        self._channel = channel
        self._bus = bus
        # Identifies THIS manager's publications so its own LISTEN loop skips them
        # (already delivered locally). Per-instance = per-process in prod (one
        # manager per channel per process); distinct per instance in tests.
        self._origin = f"{os.environ.get('HOSTNAME', 'proc')}-{uuid.uuid4().hex[:12]}"
        if bus is not None:
            bus.subscribe(channel, self._on_notify)

    async def connect(
        self, websocket: WebSocket, session_key: str = GLOBAL_SESSION
    ) -> None:
        """Accept and register a connection under a session key."""
        await websocket.accept()
        self._sessions.setdefault(session_key, []).append(websocket)
        logger.info(
            "WS client connected (session=%s) — total connections: %d",
            session_key,
            self.connection_count,
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a connection from whichever session bucket holds it."""
        for key, conns in list(self._sessions.items()):
            if websocket in conns:
                conns.remove(websocket)
                if not conns:
                    del self._sessions[key]
                break
        logger.info(
            "WS client disconnected — total connections: %d", self.connection_count
        )

    # --- public API: local-first delivery, then cross-process publish -------
    async def broadcast_to_session(
        self, session_key: str, message: dict[str, Any]
    ) -> None:
        """Push a message to the connections in one session — this process's
        sockets immediately, and (best-effort) every other process's via the bus.

        This is the isolation primitive: one user's draft events never reach
        another user's clients.
        """
        await self._local_broadcast_to_session(session_key, message)
        await self._publish(session_key, message)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Push a JSON message to ALL connected clients (every session) across
        every process. Used by the news manager."""
        await self._local_broadcast(message)
        await self._publish(None, message)

    # --- cross-process transport --------------------------------------------
    async def _publish(
        self, session_key: Optional[str], message: dict[str, Any]
    ) -> None:
        """Best-effort fan-out to other processes. Never raises — local delivery
        already happened, so a bus hiccup can't break the live rec on this box."""
        if self._bus is None or not self._bus.is_running:
            return
        try:
            payload = json.dumps(
                {"origin": self._origin, "session_key": session_key, "message": message},
                default=str,
            )
            if len(payload.encode("utf-8")) > NOTIFY_MAX_BYTES:
                logger.warning(
                    "WS message (%d bytes) exceeds the NOTIFY limit — cross-process "
                    "delivery skipped (local delivery done). channel=%s",
                    len(payload), self._channel,
                )
                return
            await self._bus.publish(self._channel, payload)
        except Exception as exc:
            logger.warning(
                "WS pubsub publish failed (%s) — local delivery already done", exc
            )

    async def _on_notify(self, payload: str) -> None:
        """Bus callback: deliver a message another process published to OUR local
        sockets. Skips our own publications (already delivered locally)."""
        try:
            data = json.loads(payload)
        except Exception:
            return
        if data.get("origin") == self._origin:
            return
        message = data.get("message")
        if message is None:
            return
        session_key = data.get("session_key")
        if session_key is None:
            await self._local_broadcast(message)
        else:
            await self._local_broadcast_to_session(session_key, message)

    # --- local delivery (the original, per-process socket writes) -----------
    async def _local_broadcast_to_session(
        self, session_key: str, message: dict[str, Any]
    ) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._sessions.get(session_key, [])):
            try:
                await ws.send_json(message)
            except Exception as exc:
                logger.warning("WebSocket send failed (%s) — removing connection", exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def _local_broadcast(self, message: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in self.active_connections:
            try:
                await ws.send_json(message)
            except Exception as exc:
                logger.warning("WebSocket send failed (%s) — removing connection", exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def active_connections(self) -> list[WebSocket]:
        """Flat list of every connection across all sessions (this process)."""
        return [ws for conns in self._sessions.values() for ws in conns]

    @property
    def connection_count(self) -> int:
        return len(self.active_connections)

    def session_connection_count(self, session_key: str) -> int:
        return len(self._sessions.get(session_key, []))


class SessionScopedBroadcaster:
    """Adapter handed to a LiveDraftEngine in place of the global ws_manager.

    Exposes the same `.broadcast(msg)` the engine already calls, but routes it to
    one session — so the engine code (and its unit tests) need no change while its
    output reaches only that draft's clients.
    """

    def __init__(self, manager: WebSocketManager, session_key: str):
        self._manager = manager
        self._session_key = session_key

    async def broadcast(self, message: dict[str, Any]) -> None:
        await self._manager.broadcast_to_session(self._session_key, message)


# ---------------------------------------------------------------------------
# The cross-process bus + the two manager singletons.
# ---------------------------------------------------------------------------
# Publishing goes through the request pool (a quick pg_notify, released at once) so
# it adds no permanent connections; the LISTEN loop uses ONE dedicated connection
# per process (see pubsub.py). start()/stop() are driven by the app lifespan.
from backend.config import settings  # noqa: E402
from backend.websocket.pubsub import PostgresPubSub, asyncpg_dsn  # noqa: E402


async def _pg_notify_via_pool(channel: str, payload: str) -> None:
    """Issue a NOTIFY through the app's request pool (brief hold, then released)."""
    from sqlalchemy import text

    from backend.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": channel, "payload": payload},
        )
        await session.commit()


pubsub: PostgresPubSub = PostgresPubSub(
    asyncpg_dsn(settings.database_url), publish_exec=_pg_notify_via_pool
)

ws_manager = WebSocketManager(channel="ws_draft", bus=pubsub)       # draft (session-keyed)
news_ws_manager = WebSocketManager(channel="ws_news", bus=pubsub)   # news (broadcast-all)
