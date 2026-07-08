"""Tests for scripts/backfill_kicker_gsis.py — idempotent gsis backfill on kicker
rows from the nflverse id crosswalk (matching sleeper_id then sportradar_id)."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from scripts.backfill_kicker_gsis import backfill_kicker_gsis


def _player(name, team, position="K", sleeper_id=None, sportradar_id=None, gsis_id=None):
    p = MagicMock()
    p.id = uuid.uuid4()
    p.name = name
    p.team_abbr = team
    p.position = position
    p.sleeper_id = sleeper_id
    p.sportradar_id = sportradar_id
    p.gsis_id = gsis_id
    return p


def _mock_session(rows: list):
    """Async session whose SELECT returns `rows` (already filtered to null-gsis K
    by the real query; the mock just hands them back)."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


# nflverse crosswalk: Aubrey resolvable by sleeper, Sanders only by sportradar,
# Smyth (UDFA) absent entirely.
_BRIDGE = pd.DataFrame([
    {"gsis_id": "00-0037692", "sleeper_id": "11533", "sportradar_id": "sr-aubrey"},
    {"gsis_id": "00-0034794", "sleeper_id": None, "sportradar_id": "sr-sanders"},
])


@pytest.mark.asyncio
async def test_backfill_fills_by_sleeper_then_sportradar():
    rows = [
        _player("Brandon Aubrey", "DAL", sleeper_id="11533", sportradar_id="sr-aubrey"),
        _player("Jason Sanders", "MIA", sleeper_id="99999", sportradar_id="sr-sanders"),
        _player("Charlie Smyth", "NO", sleeper_id="11653", sportradar_id="sr-smyth"),
    ]
    session = _mock_session(rows)
    with patch("backend.integrations.nfl_weekly.load_id_bridge", return_value=_BRIDGE):
        res = await backfill_kicker_gsis(dry_run=False, db=session)

    assert res["candidates"] == 3
    assert res["filled"] == 2
    assert res["by_sleeper"] == 1 and res["by_sportradar"] == 1
    assert rows[0].gsis_id == "00-0037692"        # Aubrey via sleeper
    assert rows[1].gsis_id == "00-0034794"        # Sanders via sportradar (no sleeper hit)
    assert rows[2].gsis_id is None                # Smyth not in crosswalk -> left null
    assert res["still_null"] == ["Charlie Smyth (NO)"]
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing():
    rows = [_player("Brandon Aubrey", "DAL", sleeper_id="11533")]
    session = _mock_session(rows)
    with patch("backend.integrations.nfl_weekly.load_id_bridge", return_value=_BRIDGE):
        res = await backfill_kicker_gsis(dry_run=True, db=session)

    assert res["filled"] == 1                     # would fill
    assert rows[0].gsis_id is None                # but did NOT mutate
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_is_idempotent_second_run_fills_zero():
    # A second run sees no null-gsis rows (the SELECT filters WHERE gsis IS NULL),
    # so there are zero candidates and nothing is filled.
    session = _mock_session([])
    with patch("backend.integrations.nfl_weekly.load_id_bridge", return_value=_BRIDGE):
        res = await backfill_kicker_gsis(dry_run=False, db=session)
    assert res["candidates"] == 0 and res["filled"] == 0
