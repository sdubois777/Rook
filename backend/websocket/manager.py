"""
backend/websocket/manager.py

WebSocket connection manager — push-based, never polls.
Broadcasts draft events from the Playwright bridge to all connected React clients.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketManager:
    """
    Manages active WebSocket connections between FastAPI and React draft clients.

    Thread-safety note: FastAPI runs async. All mutations to active_connections
    happen inside async context on the same event loop — no explicit locks needed.
    """

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(
            "Draft client connected — total connections: %d",
            len(self.active_connections),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a disconnected WebSocket from the registry."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(
            "Draft client disconnected — total connections: %d",
            len(self.active_connections),
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """
        Push a JSON message to all connected clients.
        Dead connections are removed silently — never crash on a bad connection.
        """
        dead: list[WebSocket] = []
        for ws in list(self.active_connections):
            try:
                await ws.send_json(message)
            except Exception as exc:
                logger.warning("WebSocket send failed (%s) — removing connection", exc)
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def connection_count(self) -> int:
        return len(self.active_connections)


# Module-level singleton — shared by the draft router and the Playwright bridge
ws_manager = WebSocketManager()
