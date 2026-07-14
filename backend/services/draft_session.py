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

from sqlalchemy import select, update as sa_update
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
    # True iff an active session exists AND its last EVENT (updated_at, advanced
    # only by save/persist) is within max_idle_seconds — the "plausibly still
    # live" gate for auto-resume on a page refresh.
    async def is_resumable(
        self, user_id: uuid.UUID, max_idle_seconds: int
    ) -> bool: ...
    # Durably deactivate rows whose last event is older than ttl_seconds (cold
    # abandoned drafts the warm-memory reaper never touches). Returns the count.
    async def deactivate_stale(self, ttl_seconds: int) -> int: ...


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

    async def is_resumable(
        self, user_id: uuid.UUID, max_idle_seconds: int
    ) -> bool:
        # Compare against the DB's own clock (updated_at is server now()), so the
        # recency check is independent of any app/DB clock skew.
        cutoff = func.now() - timedelta(seconds=int(max_idle_seconds))
        async with self._session_factory() as session:
            found = (
                await session.execute(
                    select(DraftSession.id).where(
                        DraftSession.user_id == user_id,
                        DraftSession.is_active.is_(True),
                        DraftSession.updated_at >= cutoff,
                    )
                )
            ).scalar_one_or_none()
            return found is not None

    async def deactivate_stale(self, ttl_seconds: int) -> int:
        cutoff = func.now() - timedelta(seconds=int(ttl_seconds))
        async with self._session_factory() as session:
            result = await session.execute(
                sa_update(DraftSession)
                .where(
                    DraftSession.is_active.is_(True),
                    DraftSession.updated_at < cutoff,
                )
                .values(is_active=False)
            )
            await session.commit()
            return result.rowcount or 0


class InMemorySessionStore:
    """SessionStore with no persistence — for tests and single-process scratch use.

    Records carry {snapshot, updated_at, active} mirroring the DB row so the
    recency gate is testable (a test can backdate _records[uid]["updated_at"]).
    """

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, dict] = {}

    async def load(self, user_id: uuid.UUID) -> Optional[dict]:
        r = self._records.get(user_id)
        return dict(r["snapshot"]) if r and r["active"] else None

    async def save(
        self, user_id: uuid.UUID, snapshot: dict, draft_type: Optional[str]
    ) -> None:
        self._records[user_id] = {
            "snapshot": dict(snapshot),
            "updated_at": _now(),
            "active": True,
        }

    async def delete(self, user_id: uuid.UUID) -> None:
        r = self._records.get(user_id)
        if r is not None:
            r["active"] = False

    async def is_resumable(
        self, user_id: uuid.UUID, max_idle_seconds: int
    ) -> bool:
        r = self._records.get(user_id)
        if not r or not r["active"]:
            return False
        return r["updated_at"] >= _now() - timedelta(seconds=max_idle_seconds)

    async def deactivate_stale(self, ttl_seconds: int) -> int:
        cutoff = _now() - timedelta(seconds=ttl_seconds)
        n = 0
        for r in self._records.values():
            if r["active"] and r["updated_at"] < cutoff:
                r["active"] = False
                n += 1
        return n


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

    async def is_resumable(self, user_id: uuid.UUID, max_idle_seconds: int) -> bool:
        """Should a page refresh AUTO-RESUME this user's draft? True only when the
        durable row is active AND its last event is within the resume window — the
        read gate that splits 'active, recover it' from 'ended/stale, show board'.
        Keyed on the DB (not the warm session), so reads can't keep a dead draft
        warm and resumable."""
        return await self._store.is_resumable(user_id, max_idle_seconds)

    async def deactivate_stale_rows(self, ttl_seconds: int) -> int:
        """Durably flip is_active=False for long-idle rows (incl. COLD ones never
        held warm in this process). The DB-flip backstop behind the read gate."""
        return await self._store.deactivate_stale(ttl_seconds)

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

    async def evict_finished_and_stale(self, safety_ttl_seconds: int) -> dict:
        """Smarter memory reaper (the load-test follow-up): distinguish a FINISHED
        draft from a merely IDLE-but-LIVE one instead of one blunt idle TTL.

        - FINISHED (board full): evict warm memory IMMEDIATELY and durably mark the
          snapshot inactive — nobody needs the engine once the draft is over, and
          this is where the resident-engine pile-up actually comes from.
        - ABANDONED (idle beyond ``safety_ttl_seconds``): evict warm memory. The TTL
          is set to comfortably EXCEED a real draft's wall-clock (a 12-team/15-round
          snake at a 90s clock is ~4.5h, plus pauses), so a live-but-paused draft is
          never cold-started mid-draft.
        - LIVE (incomplete + active within the TTL): kept warm however long it runs.

        Eviction is safe — state lives in draft_sessions and get_or_rehydrate()
        rebuilds it; the only cost is a cold-start, which is exactly what we refuse
        to inflict on a live draft. Returns counts for the reaper log.
        """
        cutoff = _now() - timedelta(seconds=safety_ttl_seconds)
        finished: list[str] = []
        abandoned: list[str] = []
        for key, sess in list(self._sessions.items()):
            try:
                done = sess.state.is_draft_complete()
            except Exception:  # a malformed state must never wedge the reaper
                done = False
            if done:
                finished.append(key)
            elif sess.last_activity < cutoff:
                abandoned.append(key)

        for key in finished + abandoned:
            self._sessions.pop(key, None)
        # Finished drafts: also retire the durable row so a stray refresh can't
        # resurrect a completed draft. Abandoned rows are left to the DB backstop
        # (deactivate_stale_rows) so a wrongly-idle live draft can still resume.
        for key in finished:
            try:
                await self._store.delete(uuid.UUID(key))  # soft-delete: mark inactive
            except Exception:
                logger.warning("Finished-draft DB deactivate failed for %s", key)

        if finished or abandoned:
            logger.info(
                "Reaper: evicted %d finished + %d abandoned warm draft session(s)",
                len(finished), len(abandoned),
            )
        return {"finished": len(finished), "abandoned": len(abandoned)}

    @property
    def active_count(self) -> int:
        return len(self._sessions)
