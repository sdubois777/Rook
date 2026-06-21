"""Live-draft session isolation + durability.

Replaces the module-global `_engine`/`_state` singletons in routers/draft.py with
a per-user registry so concurrent drafts stay fully isolated, and mirrors each
session's state to the DB so a redeploy/crash mid-draft rehydrates instead of
losing the draft.

Two layers, deliberately separated so a future multi-worker deploy is a drop-in
swap rather than a re-refactor:

  - DraftSessionManager — the in-process registry of WARM, live LiveDraftEngine
    objects keyed by user_id (the hot path; single-worker today).
  - SessionStore        — the durability/portability seam that moves serializable
    STATE SNAPSHOTS (never live engines). DbSessionStore today; a RedisSessionStore
    later is the same interface. `get_or_rehydrate` rebuilds a session from a
    snapshot on a warm miss — which is exactly the post-redeploy recovery path AND
    the future multi-worker "this worker doesn't hold the session" path.

Session key = user_id (one active draft per user — a human drafts one league at a
time).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

from backend.engines.draft_state_manager import DraftStateManager
from backend.models.draft_session import DraftSession

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# SessionStore — the durability/portability seam (DB today, Redis tomorrow)
# ---------------------------------------------------------------------------


class SessionStore(Protocol):
    """Persists serializable session-state snapshots, keyed by user_id.

    Moves plain dicts (DraftStateManager.to_dict()), never live engines — that is
    what makes a future Redis-backed implementation a genuine drop-in swap.
    """

    async def load(self, user_id: uuid.UUID) -> Optional[dict]: ...
    async def save(
        self, user_id: uuid.UUID, snapshot: dict, draft_type: Optional[str]
    ) -> None: ...
    async def delete(self, user_id: uuid.UUID) -> None: ...


class DbSessionStore:
    """SessionStore backed by the draft_sessions table (one row per user)."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def load(self, user_id: uuid.UUID) -> Optional[dict]:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(DraftSession).where(
                        DraftSession.user_id == user_id,
                        DraftSession.is_active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            return dict(row.session_state) if row and row.session_state else None

    async def save(
        self, user_id: uuid.UUID, snapshot: dict, draft_type: Optional[str]
    ) -> None:
        async with self._session_factory() as session:
            stmt = pg_insert(DraftSession).values(
                id=uuid.uuid4(),
                user_id=user_id,
                session_state=snapshot,
                draft_type=draft_type,
                is_active=True,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[DraftSession.user_id],
                set_={
                    "session_state": snapshot,
                    "draft_type": draft_type,
                    "is_active": True,
                    "updated_at": func.now(),
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def delete(self, user_id: uuid.UUID) -> None:
        """Soft-delete: mark inactive so load() stops rehydrating it. A new draft
        for the same user upserts is_active=True again."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(DraftSession).where(DraftSession.user_id == user_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                row.is_active = False
                await session.commit()


class InMemorySessionStore:
    """SessionStore with no persistence — for tests and single-process scratch use."""

    def __init__(self) -> None:
        self._data: dict[uuid.UUID, dict] = {}

    async def load(self, user_id: uuid.UUID) -> Optional[dict]:
        snap = self._data.get(user_id)
        return dict(snap) if snap else None

    async def save(
        self, user_id: uuid.UUID, snapshot: dict, draft_type: Optional[str]
    ) -> None:
        self._data[user_id] = dict(snapshot)

    async def delete(self, user_id: uuid.UUID) -> None:
        self._data.pop(user_id, None)


# ---------------------------------------------------------------------------
# DraftSessionManager — the in-process registry of warm engines
# ---------------------------------------------------------------------------

# async (state, session_key) -> engine
EngineFactory = Callable[[DraftStateManager, str], Awaitable[object]]


@dataclass
class LiveSession:
    engine: object
    state: DraftStateManager
    last_activity: datetime


class DraftSessionManager:
    """Per-user registry of warm live-draft engines + durable mirror.

    Replaces the module-global engine/state. Every entry point resolves its
    session by user_id, so two concurrent drafts never touch each other's state.
    """

    def __init__(self, store: SessionStore, engine_factory: EngineFactory):
        self._store = store
        self._engine_factory = engine_factory
        self._sessions: dict[str, LiveSession] = {}

    @staticmethod
    def _key(user_id: uuid.UUID | str) -> str:
        return str(user_id)

    async def create(
        self, user_id: uuid.UUID, state: DraftStateManager
    ) -> LiveSession:
        """Start (or restart) a user's session from a freshly built state."""
        key = self._key(user_id)
        engine = await self._engine_factory(state, key)
        sess = LiveSession(engine=engine, state=state, last_activity=_now())
        self._sessions[key] = sess
        await self._store.save(
            user_id, state.to_dict(), state.league_config.draft_type
        )
        logger.info("Draft session created for user %s", key)
        return sess

    def get_warm(self, user_id: uuid.UUID) -> Optional[LiveSession]:
        """Return the in-memory session without touching the store (or None)."""
        return self._sessions.get(self._key(user_id))

    async def get_or_rehydrate(
        self, user_id: uuid.UUID
    ) -> Optional[LiveSession]:
        """Return the warm session, else rebuild it from the durable snapshot.

        On a warm miss (process restart wiped memory, or — in a future multi-worker
        world — the session lives on another worker) this loads the snapshot and
        rebuilds an identical engine. Returns None only when there is genuinely no
        active session for the user.
        """
        key = self._key(user_id)
        warm = self._sessions.get(key)
        if warm is not None:
            warm.last_activity = _now()
            return warm

        snapshot = await self._store.load(user_id)
        if not snapshot:
            return None

        state = DraftStateManager.from_dict(snapshot)
        engine = await self._engine_factory(state, key)
        sess = LiveSession(engine=engine, state=state, last_activity=_now())
        self._sessions[key] = sess
        logger.info("Draft session rehydrated from snapshot for user %s", key)
        return sess

    async def persist(self, user_id: uuid.UUID) -> None:
        """Mirror the warm session's current state to the store (call after each
        mutating event — synchronous, one upsert; the durability guarantee)."""
        sess = self._sessions.get(self._key(user_id))
        if sess is None:
            return
        await self._store.save(
            user_id, sess.state.to_dict(), sess.state.league_config.draft_type
        )

    async def end(self, user_id: uuid.UUID) -> None:
        """Tear down a user's session (evict warm + mark the snapshot inactive)."""
        self._sessions.pop(self._key(user_id), None)
        await self._store.delete(user_id)
        logger.info("Draft session ended for user %s", self._key(user_id))

    def evict_stale(self, ttl_seconds: int) -> int:
        """Drop warm sessions idle longer than ttl (memory reaper). The durable
        snapshot is left intact until /end, so an evicted-then-resumed draft still
        rehydrates."""
        cutoff = _now() - timedelta(seconds=ttl_seconds)
        stale = [k for k, s in self._sessions.items() if s.last_activity < cutoff]
        for k in stale:
            del self._sessions[k]
        if stale:
            logger.info("Evicted %d stale warm draft session(s)", len(stale))
        return len(stale)

    @property
    def active_count(self) -> int:
        return len(self._sessions)
