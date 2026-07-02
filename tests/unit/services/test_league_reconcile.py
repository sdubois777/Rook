"""Tests for LeagueReconciler — tier-cap suspend/restore logic (fake repo)."""
from __future__ import annotations

import uuid

import pytest

from backend.core.exceptions import ValidationError
from backend.services.league_reconcile import LeagueReconciler


class _Lg:
    def __init__(self, suspended_at=None, created=0):
        self.id = uuid.uuid4()
        self.suspended_at = suspended_at
        self.created_at = created


class FakeRepo:
    """Models CURRENT-SEASON leagues only (finished are excluded by construction —
    the reconciler never sees them)."""

    def __init__(self, leagues):
        self._lg = leagues
        self.set_calls = []

    async def get_active_leagues(self, user_id):
        return [lg for lg in self._lg if lg.suspended_at is None]

    async def get_suspended_leagues(self, user_id):
        return sorted(
            [lg for lg in self._lg if lg.suspended_at is not None],
            key=lambda lg: lg.suspended_at,
        )

    async def get_current_season_leagues(self, user_id):
        return list(self._lg)

    async def set_suspended(self, user_id, league_ids, suspended_at):
        self.set_calls.append((list(league_ids), suspended_at))
        for lg in self._lg:
            if lg.id in league_ids:
                lg.suspended_at = suspended_at


UID = uuid.uuid4()


# ── reconcile_for_tier ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drop_does_not_autopark():
    # 3 active leagues, drop to standard (cap 2): reconcile parks NOTHING.
    lg = [_Lg(), _Lg(), _Lg()]
    repo = FakeRepo(lg)
    await LeagueReconciler(repo).reconcile_for_tier(UID, "standard")
    assert all(x.suspended_at is None for x in lg)   # over-limit stays computed
    assert repo.set_calls == []


@pytest.mark.asyncio
async def test_rise_restores_parked_up_to_cap():
    # 1 active + 2 parked; rise to standard (cap 2) restores exactly 1 (longest-parked).
    active = _Lg()
    parked_old = _Lg(suspended_at=1)
    parked_new = _Lg(suspended_at=2)
    repo = FakeRepo([active, parked_old, parked_new])
    await LeagueReconciler(repo).reconcile_for_tier(UID, "standard")
    assert active.suspended_at is None
    assert parked_old.suspended_at is None   # longest-parked restored first
    assert parked_new.suspended_at == 2      # still parked (cap reached)


@pytest.mark.asyncio
async def test_pro_restores_everything():
    parked = [_Lg(suspended_at=1), _Lg(suspended_at=2), _Lg(suspended_at=3)]
    repo = FakeRepo([_Lg(), *parked])
    await LeagueReconciler(repo).reconcile_for_tier(UID, "pro")  # unlimited
    assert all(x.suspended_at is None for x in parked)


@pytest.mark.asyncio
async def test_rise_no_room_restores_nothing():
    # 2 active at cap 2, 1 parked → no room.
    repo = FakeRepo([_Lg(), _Lg(), _Lg(suspended_at=1)])
    await LeagueReconciler(repo).reconcile_for_tier(UID, "standard")
    assert repo.set_calls == []


# ── limit_state ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_limit_state_over():
    repo = FakeRepo([_Lg(), _Lg(), _Lg()])
    state = await LeagueReconciler(repo).limit_state(UID, "standard")
    assert state["over_limit"] is True
    assert state["active_count"] == 3
    assert state["max_leagues"] == 2
    assert len(state["candidates"]) == 3


@pytest.mark.asyncio
async def test_limit_state_pro_never_over():
    repo = FakeRepo([_Lg(), _Lg(), _Lg(), _Lg()])
    state = await LeagueReconciler(repo).limit_state(UID, "pro")
    assert state["over_limit"] is False
    assert state["max_leagues"] is None


# ── resolve_keep ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_keep_parks_the_rest():
    a, b, c = _Lg(), _Lg(), _Lg()
    repo = FakeRepo([a, b, c])
    await LeagueReconciler(repo).resolve_keep(UID, "standard", [a.id, b.id])
    assert a.suspended_at is None and b.suspended_at is None
    assert c.suspended_at is not None   # parked, not deleted


@pytest.mark.asyncio
async def test_resolve_keep_is_idempotent_and_can_swap():
    a, b, c = _Lg(), _Lg(), _Lg()
    r = LeagueReconciler(FakeRepo([a, b, c]))
    await r.resolve_keep(UID, "standard", [a.id, b.id])
    # Re-choose to swap: keep a + c instead → b parks, c restored.
    await r.resolve_keep(UID, "standard", [a.id, c.id])
    assert a.suspended_at is None and c.suspended_at is None
    assert b.suspended_at is not None


@pytest.mark.asyncio
async def test_resolve_keep_over_cap_rejected():
    a, b, c = _Lg(), _Lg(), _Lg()
    repo = FakeRepo([a, b, c])
    with pytest.raises(ValidationError):
        await LeagueReconciler(repo).resolve_keep(UID, "standard", [a.id, b.id, c.id])


@pytest.mark.asyncio
async def test_resolve_keep_unknown_id_rejected():
    a = _Lg()
    repo = FakeRepo([a])
    with pytest.raises(ValidationError):
        await LeagueReconciler(repo).resolve_keep(UID, "standard", [uuid.uuid4()])
