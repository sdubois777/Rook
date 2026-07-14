"""Cross-process WebSocket fan-out via Postgres LISTEN/NOTIFY. NO Redis.

The WS delivery map (``WebSocketManager._sessions``) is PER-PROCESS — it only knows
the sockets THIS process holds. When a message is produced on process A but the
target socket lives on process B, it must cross the process boundary. This bus is
that bridge: ``publish()`` issues a Postgres ``NOTIFY``; every process runs a
``LISTEN`` loop and hands each notification to the local manager, which delivers to
whichever sockets IT holds.

Seam (narrow + named so a Redis transport could drop in later without touching
callers): ``PubSubBackend`` = ``subscribe`` / ``publish`` / ``start`` / ``stop``.
``PostgresPubSub`` is the only implementation today.

Connection budget: ONE dedicated ``LISTEN`` connection per process, OUTSIDE the
request pool (a raw asyncpg connection — the pool can't run ``add_listener``).
``NOTIFY`` goes THROUGH the request pool (a quick ``pg_notify`` that's released
immediately), so publishing adds no permanent connections.

Robustness: the ``LISTEN`` connection is long-lived, and Railway drops idle
connections — so the loop heartbeats and RE-ESTABLISHES on any drop (the most
likely production failure of this design). ``NOTIFY`` is fire-and-forget: a message
published while no process is listening is gone — acceptable, because a socket is
either connected (and its process is listening) or it isn't, and a reconnecting
client rehydrates its draft state from the DB (``get_or_rehydrate``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional, Protocol

import asyncpg

logger = logging.getLogger(__name__)

Handler = Callable[[str], Awaitable[None]]

# NOTIFY payload hard limit is 8000 bytes; publishers stay under this with margin.
NOTIFY_MAX_BYTES = 7900
_HEARTBEAT_SECONDS = 30                       # keepalive + fast drop-detection
_RECONNECT_BACKOFF = (1, 2, 5, 10, 20, 30)    # seconds, capped


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    """Turn a SQLAlchemy ``postgresql+asyncpg://…`` URL into a raw asyncpg DSN."""
    return sqlalchemy_url.replace("+asyncpg", "", 1)


class PubSubBackend(Protocol):
    def subscribe(self, channel: str, handler: Handler) -> None: ...
    async def publish(self, channel: str, payload: str) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    @property
    def is_running(self) -> bool: ...


class PostgresPubSub:
    """LISTEN/NOTIFY transport. One dedicated LISTEN connection per process; NOTIFY
    via an injected executor (the app pool in prod, a stub in tests)."""

    def __init__(
        self, dsn: str, *, publish_exec: Callable[[str, str], Awaitable[None]] | None = None
    ):
        self._dsn = dsn
        self._publish_exec = publish_exec
        self._handlers: dict[str, Handler] = {}
        self._task: Optional[asyncio.Task] = None
        self._conn: Optional[asyncpg.Connection] = None
        self._stopped = asyncio.Event()
        # Set by asyncpg's termination listener the instant the LISTEN connection
        # drops — so we reconnect in ~1s instead of waiting out the heartbeat.
        self._conn_lost = asyncio.Event()
        self._running = False

    # --- seam ---------------------------------------------------------------
    def subscribe(self, channel: str, handler: Handler) -> None:
        """Register a channel handler. Call BEFORE start() (channels are LISTENed
        when the loop connects). Idempotent per channel."""
        self._handlers[channel] = handler

    async def publish(self, channel: str, payload: str) -> None:
        if self._publish_exec is None:
            raise RuntimeError("PostgresPubSub has no publish executor configured")
        await self._publish_exec(channel, payload)

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._running = True
        self._task = asyncio.create_task(self._run(), name="ws-pubsub-listen")

    async def stop(self) -> None:
        self._running = False
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_conn()

    # --- internals ----------------------------------------------------------
    async def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None and not conn.is_closed():
            try:
                await conn.close(timeout=5)
            except Exception:
                conn.terminate()

    def _dispatch(self, _conn, _pid, channel: str, payload: str) -> None:
        """asyncpg listener callback (sync) — hand off to the async handler."""
        handler = self._handlers.get(channel)
        if handler is not None:
            asyncio.create_task(handler(payload))

    def _on_conn_terminated(self, _conn) -> None:
        """asyncpg termination listener (sync) — fires the instant the LISTEN
        connection is lost, so the loop reconnects at once instead of waiting out
        the heartbeat interval. This is the fast path for a Railway idle-drop."""
        self._conn_lost.set()

    async def _run(self) -> None:
        """Supervised LISTEN loop: (re)connect, register listeners, heartbeat; on
        any drop, log and reconnect with backoff. This is what survives Railway
        dropping the idle connection."""
        backoff_i = 0
        while not self._stopped.is_set():
            try:
                self._conn_lost.clear()
                self._conn = await asyncpg.connect(self._dsn)
                self._conn.add_termination_listener(self._on_conn_terminated)
                for channel in self._handlers:
                    await self._conn.add_listener(channel, self._dispatch)
                logger.info(
                    "WS pubsub LISTEN established (channels=%s)", list(self._handlers)
                )
                backoff_i = 0
                while not self._stopped.is_set():
                    # Wake on EITHER the heartbeat interval OR an immediate drop.
                    try:
                        await asyncio.wait_for(
                            self._conn_lost.wait(), timeout=_HEARTBEAT_SECONDS
                        )
                        raise ConnectionError("LISTEN connection terminated")
                    except asyncio.TimeoutError:
                        pass  # heartbeat tick — validate the connection below
                    if self._conn is None or self._conn.is_closed():
                        raise ConnectionError("LISTEN connection closed")
                    await self._conn.execute("SELECT 1")  # raises if the conn is dead
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stopped.is_set():
                    break
                wait = _RECONNECT_BACKOFF[min(backoff_i, len(_RECONNECT_BACKOFF) - 1)]
                logger.warning(
                    "WS pubsub LISTEN lost (%s) — reconnecting in %ss", exc, wait
                )
                backoff_i += 1
                await self._close_conn()
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        await self._close_conn()
