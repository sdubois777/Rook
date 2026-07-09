"""
Tests for the deterministic availability pass (engines/availability_pass.py) — the
DB write of availability_factor/games_missed from Sleeper's structured status, and
idempotency. Uses a fake session (no DB, no Sleeper fetch).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from backend.engines.availability_pass import apply_availability_discounts


class _P:
    """A Player-ish row the pass reads/writes."""
    def __init__(self, sleeper_id, name="X", position="WR"):
        self.sleeper_id = sleeper_id
        self.name = name
        self.position = position
        self.availability_factor = Decimal("1.000")
        self.availability_games_missed = 0
        self.availability_reason = None


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _FakeDB:
    def __init__(self, players):
        self._players = players
        self.commits = 0

    async def execute(self, *_a, **_k):
        return _Res(self._players)

    async def commit(self):
        self.commits += 1


async def test_pass_discounts_ir_and_leaves_healthy():
    ir = _P("s_ir", "IR Guy")
    healthy = _P("s_ok", "Healthy Guy")
    db = _FakeDB([ir, healthy])
    status_map = {
        "s_ir": ("Inactive", "IR"),        # rostered IR → ir_long
        "s_ok": ("Active", None),          # healthy
    }
    res = await apply_availability_discounts(db, status_by_sleeper_id=status_map)
    assert res["discounted"] == 1 and res["total"] == 2
    assert ir.availability_factor < Decimal("1.000") and ir.availability_games_missed == 13
    assert ir.availability_reason and "ir_long" in ir.availability_reason
    assert healthy.availability_factor == Decimal("1.000") and healthy.availability_games_missed == 0
    assert healthy.availability_reason is None


async def test_pass_prorates_pup_and_suspension_distinctly():
    pup = _P("s_pup")
    susp = _P("s_susp")
    db = _FakeDB([pup, susp])
    status_map = {
        "s_pup": ("Physically Unable to Perform", None),
        "s_susp": ("Suspended", None),
    }
    await apply_availability_discounts(db, status_by_sleeper_id=status_map)
    assert pup.availability_games_missed == 6 and pup.availability_factor < Decimal("1.000")
    assert susp.availability_games_missed == 4 and susp.availability_factor < Decimal("1.000")


async def test_pass_is_idempotent():
    ir = _P("s_ir")
    db = _FakeDB([ir])
    status_map = {"s_ir": ("Inactive", "IR")}
    r1 = await apply_availability_discounts(db, status_by_sleeper_id=status_map)
    f1, g1 = ir.availability_factor, ir.availability_games_missed
    r2 = await apply_availability_discounts(db, status_by_sleeper_id=status_map)
    assert r2["updated"] == 0                     # nothing changed on the second run
    assert ir.availability_factor == f1 and ir.availability_games_missed == g1


async def test_pass_resets_recovered_player_to_full():
    """A player who was discounted last run but is now healthy resets to 1.000."""
    p = _P("s1")
    p.availability_factor = Decimal("0.315")     # stale discount from a prior run
    p.availability_games_missed = 13
    p.availability_reason = "ir_long: ..."
    db = _FakeDB([p])
    res = await apply_availability_discounts(db, status_by_sleeper_id={"s1": ("Active", None)})
    assert res["updated"] == 1
    assert p.availability_factor == Decimal("1.000") and p.availability_games_missed == 0
    assert p.availability_reason is None
