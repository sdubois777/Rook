"""
backend/websocket/manager.py

WebSocket connection manager — push-based, never polls.

Connections are grouped by a session key so draft events reach ONLY the clients
watching that draft (per-user isolation): `broadcast_to_session(key, msg)`.
`broadcast(msg)` still fans out to every connection — used by the news manager,
where all clients should receive every signal.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

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

    def __init__(self) -> None:
        # session_key -> list of connections for that session
        self._sessions: dict[str, list[WebSocket]] = {}

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

    async def broadcast_to_session(
        self, session_key: str, message: dict[str, Any]
    ) -> None:
        """Push a message to only the connections in one session.

        This is the isolation primitive: one user's draft events never reach
        another user's clients.
        """
        dead: list[WebSocket] = []
        for ws in list(self._sessions.get(session_key, [])):
            try:
                await ws.send_json(message)
            except Exception as exc:
                logger.warning("WebSocket send failed (%s) — removing connection", exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """
        Push a JSON message to ALL connected clients (every session).
        Used by the news manager. Dead connections are removed silently.
        """
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
        """Flat list of every connection across all sessions."""
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


# Module-level singletons
ws_manager = WebSocketManager()        # Draft events (session-keyed)
news_ws_manager = WebSocketManager()   # News/beat reporter signals (broadcast-all)
