"""Unit tests for pipeline triggers — affected-set derivation, draft-window gate,
and the event debouncer. DB-free: a tiny fake session returns canned results so the
derivation LOGIC (reasons, dedup, same-position filter) is tested deterministically.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.services import pipeline_triggers as pt
from backend.services.pipeline_triggers import (
    TargetedRefreshDebouncer,
    derive_affected_set,
    is_draft_window_active,
)


# --- lightweight fakes -------------------------------------------------------
class _P:
    def __init__(self, name, team, pos, depth=1):
        self.id = uuid.uuid4()
        self.name = name
        self.team_abbr = team
        self.position = pos
        self.depth_chart_order = depth


class _Dep:
    def __init__(self, player_id, flag_type, effect, cond, reasoning):
        self.player_id = player_id
        self.flag_type = flag_type
        self.effect_on_value = effect
        self.trigger_condition = cond
        self.reasoning = reasoning


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]


class _Session:
    """Returns pre-seeded results in FIFO order of execute() calls."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        return self._results.pop(0)


# --- PART 2: affected-set derivation -----------------------------------------
@pytest.mark.asyncio
async def test_injury_derives_dependency_edges_plus_depth_chart():
    """A WR injury: the affected set = the player + his dependency-edge dependents +
    same-team/same-position depth room. Dependents that are ALSO on the depth chart
    dedup to ONE entry carrying BOTH reasons — proving the set is derived, not a list."""
    jefferson = _P("Justin Jefferson", "MIN", "WR")
    addison = _P("Jordan Addison", "MIN", "WR")     # dependency edge AND depth mate
    backup = _P("Jalen Nailor", "MIN", "WR")        # depth mate only
    dep = _Dep(addison.id, "displaced", "positive", "injured",
               "Addison's target share rises when Jefferson is out")

    session = _Session([
        _Result([dep]),          # (b) PlayerDependency where trigger = Jefferson
        _Result([addison]),      # (b) resolve the dependent player
        _Result([addison, backup]),  # (c) same team+pos depth room
    ])

    out = await derive_affected_set(session, jefferson, pt.INJURY)
    by_name = {e["player"].name: e["reasons"] for e in out["affected"]}

    assert set(by_name) == {"Justin Jefferson", "Jordan Addison", "Jalen Nailor"}
    # trigger reason
    assert any("directly involved" in r for r in by_name["Justin Jefferson"])
    # Addison carries BOTH a dependency-edge reason and a depth-chart reason
    assert any("dependency edge" in r for r in by_name["Jordan Addison"])
    assert any("depth chart" in r for r in by_name["Jordan Addison"])
    # backup is depth-chart only
    assert by_name["Jalen Nailor"] == [
        r for r in by_name["Jalen Nailor"] if "depth chart" in r
    ]


@pytest.mark.asyncio
async def test_signing_derives_new_team_displacement():
    """A signing crowds the DESTINATION team's same-position room (arrival branch),
    not a vacated room."""
    arrival = _P("Free Agent WR", None, "WR")
    incumbent = _P("Incumbent WR", "NYJ", "WR")
    session = _Session([
        _Result([]),               # no dependency edges
        _Result([incumbent]),      # (d) same-position room on the new team
    ])

    out = await derive_affected_set(session, arrival, pt.SIGNING, new_team="NYJ")
    names = {e["player"].name for e in out["affected"]}
    assert names == {"Free Agent WR", "Incumbent WR"}
    incumbent_reasons = next(
        e["reasons"] for e in out["affected"] if e["player"].name == "Incumbent WR"
    )
    assert any("crowds the same position" in r for r in incumbent_reasons)


# --- PART 3: draft-window gate -----------------------------------------------
@pytest.mark.asyncio
async def test_draft_window_active_when_live_session_present():
    session = _Session([_Result([2])])   # live count = 2 (short-circuits before scheduled)
    active, reason = await is_draft_window_active(session)
    assert active is True
    assert "live draft session" in reason


@pytest.mark.asyncio
async def test_draft_window_active_when_draft_scheduled():
    session = _Session([_Result([0]), _Result([3])])  # live=0, scheduled=3
    active, reason = await is_draft_window_active(session)
    assert active is True
    assert "scheduled" in reason


@pytest.mark.asyncio
async def test_draft_window_inactive_when_no_drafts():
    session = _Session([_Result([0]), _Result([0])])
    active, reason = await is_draft_window_active(session)
    assert active is False
    assert reason == "no live or imminent drafts"


# --- PART 3: debounce --------------------------------------------------------
@pytest.mark.asyncio
async def test_burst_about_one_player_fires_one_refresh():
    """Five enqueues for one player → ONE refresh call carrying that player."""
    calls: list[tuple[set, str]] = []

    async def refresh(ids, event_type):
        calls.append((set(ids), event_type))

    deb = TargetedRefreshDebouncer(refresh, delay_seconds=0)
    pid = uuid.uuid4()
    for _ in range(5):
        deb.enqueue({pid}, "injury")

    await asyncio.sleep(0.05)  # let the single timer fire
    assert len(calls) == 1
    assert calls[0] == ({pid}, "injury")


@pytest.mark.asyncio
async def test_burst_coalesces_multiple_players_into_one_refresh():
    calls: list[set] = []

    async def refresh(ids, event_type):
        calls.append(set(ids))

    deb = TargetedRefreshDebouncer(refresh, delay_seconds=0)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    deb.enqueue({a}, "trade")
    deb.enqueue({b, c}, "trade")

    await asyncio.sleep(0.05)
    assert len(calls) == 1
    assert calls[0] == {a, b, c}


@pytest.mark.asyncio
async def test_second_burst_after_first_fires_again():
    calls: list[set] = []

    async def refresh(ids, event_type):
        calls.append(set(ids))

    deb = TargetedRefreshDebouncer(refresh, delay_seconds=0)
    a, b = uuid.uuid4(), uuid.uuid4()
    deb.enqueue({a})
    await asyncio.sleep(0.05)
    deb.enqueue({b})
    await asyncio.sleep(0.05)
    assert calls == [{a}, {b}]
