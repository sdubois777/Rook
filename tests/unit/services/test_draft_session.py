"""DraftSessionManager + SessionStore — registry, isolation, rehydration.

Uses InMemorySessionStore and a fake engine_factory so these are pure unit tests
of the session machinery (the rehydration-CORRECTNESS test with a real engine and
identical /recommendation output lives in the router-level draft tests).
"""
from __future__ import annotations

import uuid

import pytest

from backend.engines.draft_state_manager import (
    DraftPick,
    DraftStateManager,
    LeagueConfig,
)
from backend.services.draft_session import (
    DraftSessionManager,
    InMemorySessionStore,
    LiveSession,
)


class _FakeEngine:
    """Minimal engine stand-in carrying its state + session key for assertions."""

    def __init__(self, state, session_key):
        self.state = state
        self.session_key = session_key


async def _factory(state, session_key):
    return _FakeEngine(state, session_key)


def _state(team_id="my_team", budget=200) -> DraftStateManager:
    return DraftStateManager(LeagueConfig(auction_budget=budget), team_id)


def _mgr() -> DraftSessionManager:
    return DraftSessionManager(InMemorySessionStore(), _factory)


@pytest.mark.asyncio
async def test_create_registers_warm_session_and_persists():
    mgr = _mgr()
    uid = uuid.uuid4()
    sess = await mgr.create(uid, _state())
    assert isinstance(sess, LiveSession)
    assert mgr.get_warm(uid) is sess
    assert sess.engine.session_key == str(uid)
    assert mgr.active_count == 1


@pytest.mark.asyncio
async def test_two_users_are_fully_isolated():
    """THE bug: two concurrent sessions must not share engine/state."""
    mgr = _mgr()
    a, b = uuid.uuid4(), uuid.uuid4()
    sa = await mgr.create(a, _state("team_a"))
    sb = await mgr.create(b, _state("team_b"))

    # Distinct objects.
    assert sa is not sb
    assert sa.state is not sb.state
    assert sa.engine is not sb.engine

    # A's picks never appear in B's state.
    sa.state.record_pick(DraftPick("nfl_1", "team_a", 50, "Bijan", "RB"))
    assert len(sa.state.your_roster) == 1
    assert len(sb.state.your_roster) == 0
    assert mgr.get_warm(a).state.your_team_id == "team_a"
    assert mgr.get_warm(b).state.your_team_id == "team_b"


@pytest.mark.asyncio
async def test_get_or_rehydrate_warm_hit_returns_same_object():
    mgr = _mgr()
    uid = uuid.uuid4()
    created = await mgr.create(uid, _state())
    again = await mgr.get_or_rehydrate(uid)
    assert again is created  # no rebuild on warm hit


@pytest.mark.asyncio
async def test_rehydrate_after_eviction_restores_state():
    """Simulate a redeploy: warm memory wiped, snapshot in the store, rehydrate."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()

    state = _state("team_x")
    state.record_pick(DraftPick("nfl_1", "team_x", 47, "CMC", "RB"))
    state.record_pick(DraftPick("nfl_2", "opp", 30, "Evans", "WR"))
    await mgr.create(uid, state)
    await mgr.persist(uid)  # snapshot the post-pick state

    # Wipe warm memory (the redeploy) but keep the store (DB survives).
    mgr._sessions.clear()
    assert mgr.get_warm(uid) is None

    rehydrated = await mgr.get_or_rehydrate(uid)
    assert rehydrated is not None
    assert rehydrated.state.your_team_id == "team_x"
    assert rehydrated.state.get_your_remaining_budget() == 200 - 47
    assert [p.player_id for p in rehydrated.state.your_roster] == ["nfl_1"]
    assert "opp" in rehydrated.state.opponent_rosters


@pytest.mark.asyncio
async def test_get_or_rehydrate_returns_none_when_no_session():
    mgr = _mgr()
    assert await mgr.get_or_rehydrate(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_end_evicts_and_stops_rehydration():
    mgr = _mgr()
    uid = uuid.uuid4()
    await mgr.create(uid, _state())
    await mgr.end(uid)
    assert mgr.get_warm(uid) is None
    # After end, the snapshot is inactive — no rehydration.
    assert await mgr.get_or_rehydrate(uid) is None


@pytest.mark.asyncio
async def test_evict_stale_drops_idle_warm_sessions_keeps_snapshot():
    mgr = _mgr()
    uid = uuid.uuid4()
    await mgr.create(uid, _state())
    # Force the session to look idle.
    mgr.get_warm(uid).last_activity = mgr.get_warm(uid).last_activity.replace(year=2000)

    evicted = mgr.evict_stale(ttl_seconds=3600)
    assert evicted == 1
    assert mgr.get_warm(uid) is None
    # Snapshot still present → an evicted draft can still be resumed.
    assert await mgr.get_or_rehydrate(uid) is not None


# --- smarter reaper: finished vs abandoned vs live (load-test follow-up) ---


def _tiny_config() -> LeagueConfig:
    """A 2-team, 1-slot league so a full board is exactly 2 picks."""
    return LeagueConfig(team_count=2, roster_slots={"QB": 1}, draft_type="snake")


def _complete_state() -> DraftStateManager:
    """A snake state whose board is FULL (2 teams x 1 slot = 2 drafted)."""
    s = DraftStateManager(_tiny_config(), "me")
    s.record_snake_pick("Josh Allen", is_yours=True)
    s.record_snake_pick("Jalen Hurts", is_yours=False)
    return s


def _live_incomplete_state() -> DraftStateManager:
    """A snake state mid-draft: board not yet full (1 of 2)."""
    s = DraftStateManager(_tiny_config(), "me")
    s.record_snake_pick("Josh Allen", is_yours=True)
    return s


def test_is_draft_complete_true_only_when_board_full():
    assert _complete_state().is_draft_complete() is True
    assert _live_incomplete_state().is_draft_complete() is False


@pytest.mark.asyncio
async def test_reaper_evicts_finished_immediately_and_deactivates_db():
    """A FINISHED (board-full) draft is evicted from memory at once AND its durable
    row is marked inactive — even though it was just active (not idle)."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()
    await mgr.create(uid, _complete_state())

    reaped = await mgr.evict_finished_and_stale(safety_ttl_seconds=8 * 3600)

    assert reaped == {"finished": 1, "abandoned": 0}
    assert mgr.get_warm(uid) is None            # warm engine reclaimed
    assert store._records[uid]["active"] is False  # durable row retired


@pytest.mark.asyncio
async def test_reaper_keeps_live_draft_warm_even_when_paused():
    """A LIVE (incomplete) draft is NOT evicted, even paused well beyond a normal
    between-picks gap — as long as it's within the safety TTL. Proves we never
    cold-start a live draft mid-draft."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()
    await mgr.create(uid, _live_incomplete_state())
    # Simulate a 3h pause (dinner break) — longer than the OLD 6h? no: under 8h TTL.
    sess = mgr.get_warm(uid)
    sess.last_activity = sess.last_activity - __import__("datetime").timedelta(hours=3)

    reaped = await mgr.evict_finished_and_stale(safety_ttl_seconds=8 * 3600)

    assert reaped == {"finished": 0, "abandoned": 0}
    assert mgr.get_warm(uid) is not None         # still warm mid-draft


@pytest.mark.asyncio
async def test_reaper_evicts_abandoned_incomplete_after_safety_ttl():
    """An incomplete draft idle BEYOND the safety TTL is treated as abandoned and
    evicted from memory (snapshot left for the DB backstop, so a stray resume still
    works)."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()
    await mgr.create(uid, _live_incomplete_state())
    sess = mgr.get_warm(uid)
    sess.last_activity = sess.last_activity.replace(year=2000)  # long abandoned

    reaped = await mgr.evict_finished_and_stale(safety_ttl_seconds=8 * 3600)

    assert reaped == {"finished": 0, "abandoned": 1}
    assert mgr.get_warm(uid) is None
    # Incomplete + abandoned: snapshot NOT force-retired here (DB backstop handles it)
    assert store._records[uid]["active"] is True


@pytest.mark.asyncio
async def test_create_for_second_user_does_not_attach_to_first():
    """B starting a draft while A drafts creates B's OWN session, not A's."""
    mgr = _mgr()
    a, b = uuid.uuid4(), uuid.uuid4()
    await mgr.create(a, _state("team_a"))
    sb = await mgr.create(b, _state("team_b"))
    assert sb.state.your_team_id == "team_b"
    assert mgr.get_warm(a).state.your_team_id == "team_a"
    assert mgr.active_count == 2


def _backdate(store, user_id, *, year=2000):
    """Make a stored session's last event look old (past any resume window)."""
    store._records[user_id]["updated_at"] = (
        store._records[user_id]["updated_at"].replace(year=year)
    )


# --- recency gate: resumable only when active AND recently active ---


@pytest.mark.asyncio
async def test_is_resumable_true_for_recent_active_session():
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()
    await mgr.create(uid, _state())
    assert await mgr.is_resumable(uid, 3600) is True


@pytest.mark.asyncio
async def test_is_resumable_false_when_stale():
    """Active but no event within the window → NOT resumable (abandoned draft)."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()
    await mgr.create(uid, _state())
    _backdate(store, uid)
    assert await mgr.is_resumable(uid, 3600) is False


@pytest.mark.asyncio
async def test_is_resumable_false_after_end():
    """End Draft → is_active=False → NOT resumable (the immediate-409 case)."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()
    await mgr.create(uid, _state())
    await mgr.end(uid)
    assert await mgr.is_resumable(uid, 3600) is False


@pytest.mark.asyncio
async def test_is_resumable_false_when_no_session():
    mgr = _mgr()
    assert await mgr.is_resumable(uuid.uuid4(), 3600) is False


@pytest.mark.asyncio
async def test_persist_refreshes_recency():
    """A real event (persist) re-freshens updated_at — keep-alive for live drafts."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()
    await mgr.create(uid, _state())
    _backdate(store, uid)
    assert await mgr.is_resumable(uid, 3600) is False
    await mgr.persist(uid)  # an event lands
    assert await mgr.is_resumable(uid, 3600) is True


@pytest.mark.asyncio
async def test_deactivate_stale_rows_flips_only_old_active_rows():
    """Reaper DB-flip: long-idle rows go inactive; recent ones are untouched."""
    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    old, fresh = uuid.uuid4(), uuid.uuid4()
    await mgr.create(old, _state())
    await mgr.create(fresh, _state())
    _backdate(store, old)

    n = await mgr.deactivate_stale_rows(3600)
    assert n == 1
    assert await mgr.is_resumable(old, 3600) is False
    assert await mgr.is_resumable(fresh, 3600) is True
    # The deactivated row no longer rehydrates (cold abandoned draft is closed).
    assert await store.load(old) is None


def _recommendation_inputs(state) -> dict:
    """Deterministic stand-in for the engine's recommendation, computed purely
    from the exact state fields the real LiveDraftEngine reads when it builds a
    recommendation (budget math, roster, positional counts, drafted-exclusions).

    Asserting these are identical after evict+rehydrate proves DOWNSTREAM
    BEHAVIOR is preserved — dict round-trip equality alone wouldn't catch a field
    that's restored but mis-typed/mis-derived. The LLM call itself is
    non-deterministic and out of scope; its inputs are exactly this.
    """
    return {
        "remaining_budget": state.get_your_remaining_budget(),
        "spendable_on_player": state.get_spendable_on_this_player(),
        "slots_remaining": state.get_roster_slots_remaining(),
        "min_completion": state.get_minimum_completion_budget(),
        "positional_counts": state.get_your_positional_counts(),
        "my_roster": [(p.player_id, p.price) for p in state.your_roster],
        "roster_summary": state.get_roster_summary(),
        "my_snake_roster": state.get_my_roster(),
        "opponent_budgets": dict(state.opponent_budgets),
        "cmc_excluded": state.is_drafted("Christian McCaffrey"),
        "hill_excluded": state.is_drafted("Tyreek Hill"),
        "needs": state.format_roster_needs(state.get_my_roster()),
    }


@pytest.mark.asyncio
async def test_recommendation_inputs_identical_after_evict_and_rehydrate():
    """The real durability proof: a realistic mid-auction state, evicted and
    rehydrated from the snapshot, yields IDENTICAL recommendation inputs."""
    from backend.engines.draft_state_manager import DraftPick

    store = InMemorySessionStore()
    mgr = DraftSessionManager(store, _factory)
    uid = uuid.uuid4()

    # Realistic mid-auction state: pick ~5, partial rosters/budgets, bid recovery,
    # snake exclusions.
    state = _state("my_team")
    state.record_pick(DraftPick("nfl_1", "my_team", 54, "Bijan Robinson", "RB", 1))
    state.record_pick(DraftPick("nfl_2", "my_team", 7, "Sam LaPorta", "TE", 2))
    state.record_pick(DraftPick("nfl_3", "opp_a", 61, "Ja'Marr Chase", "WR", 1))
    state.record_pick(DraftPick("nfl_4", "opp_b", 28, "Mike Evans", "WR", 2))
    state.record_my_bid("nfl_9", 22)
    state.record_snake_pick("Christian McCaffrey", "RB", 1, 1, is_yours=True)
    state.record_snake_pick("Tyreek Hill", "WR", 2, 1, is_yours=False)

    await mgr.create(uid, state)
    await mgr.persist(uid)
    before = _recommendation_inputs(mgr.get_warm(uid).state)

    # Redeploy: warm memory gone, DB snapshot survives.
    mgr._sessions.clear()
    rehydrated = await mgr.get_or_rehydrate(uid)
    assert rehydrated is not None
    after = _recommendation_inputs(rehydrated.state)

    assert after == before
