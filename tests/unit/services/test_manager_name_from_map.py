"""Stored draft picks must carry a real manager name.

All three adapters hardcode `manager_name=""` on DraftPick
(yahoo_league_api.py:251, espn_league_api.py:276, sleeper_league_api.py:165)
even though every one of them resolves real names on the ROSTER path. Because
`LeagueAuctionHistoryRepository.manager_tendencies` filters `manager_name != ""`,
opponent tendencies were permanently empty for every synced user — the map was
built ~70 lines earlier in the same sync and simply never handed down.

The join key is the load-bearing part. manager_map is keyed on
TeamRoster.platform_team_id; picks join on DraftPick.picked_by_team_id. These are
the same value on all three platforms, but they are produced by different code, so
these tests pin the format per platform. A silent mismatch would attribute a real
manager's name to the WRONG team, which is worse than leaving it blank.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.league_sync import LeagueSyncService


def _pick(team_id, name="Bijan Robinson", pid="p1"):
    return SimpleNamespace(
        platform_player_id=pid,
        player_name=name,
        position="RB",
        auction_price=55,
        manager_name="",          # what every adapter actually sends
        picked_by_team_id=team_id,
        pick_number=1,
    )


def _capturing_db():
    """Captures compiled INSERT params so the stored manager_name is assertable."""
    db = MagicMock()
    captured: list = []

    async def execute(stmt, *a, **kw):
        try:
            captured.append(stmt.compile().params)
        except Exception:
            captured.append({})
        result = MagicMock()
        result.rowcount = 1
        result.all.return_value = []
        scalars = MagicMock()
        scalars.all.return_value = []
        result.scalars.return_value = scalars
        return result

    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    db.captured = captured
    return db


async def _store(picks, manager_map):
    db = _capturing_db()
    svc = LeagueSyncService(db, uuid.uuid4())
    await svc._store_picks(picks, uuid.uuid4(), 2026, manager_map=manager_map)
    return [p for p in db.captured if "manager_name" in p]


@pytest.mark.asyncio
async def test_espn_manager_name_is_filled_from_the_roster_map():
    """ESPN: manager_map key is str(team["id"]); pick carries str(pick["teamId"])."""
    rows = await _store([_pick("3")], {"3": "Ben Dover", "7": "The Lord"})
    assert rows, "no insert captured"
    assert rows[0]["manager_name"] == "Ben Dover"


@pytest.mark.asyncio
async def test_sleeper_manager_name_is_filled_from_the_roster_map():
    """Sleeper: both sides are str(roster_id)."""
    rows = await _store([_pick("5")], {"5": "GOAT C.", "1": "PAIN"})
    assert rows[0]["manager_name"] == "GOAT C."


@pytest.mark.asyncio
async def test_yahoo_manager_name_joins_on_the_full_team_key():
    """Yahoo: both sides are the full team_key, NOT a bare index."""
    key = "461.l.78512.t.9"
    rows = await _store([_pick(key)], {key: "Fat Bastard"})
    assert rows[0]["manager_name"] == "Fat Bastard"


@pytest.mark.asyncio
async def test_yahoo_joins_across_seasons_despite_a_rotating_game_key():
    """The case a naive dict lookup silently misses on EVERY historical row.

    manager_map is built from the CURRENT season's roster call, so its keys carry
    this season's game key (470 in 2026). Historical draft results carry the game
    key of THEIR season (461 for 2025, 449 for 2024). Same league, same team slot,
    different prefix — so a direct lookup fails and every historical Yahoo pick
    would have stored a blank manager despite the map being right there.
    """
    rows = await _store(
        [_pick("461.l.78512.t.9")],           # 2025 draft result
        {"470.l.78512.t.9": "The Lord"},      # 2026 roster map
    )
    assert rows[0]["manager_name"] == "The Lord"


@pytest.mark.asyncio
async def test_a_different_league_does_not_match_across_seasons():
    """The season-tolerant fallback must not become a loose match.

    Same team slot, DIFFERENT league id — must stay blank.
    """
    rows = await _store(
        [_pick("461.l.99999.t.9")],
        {"470.l.78512.t.9": "The Lord"},
    )
    assert rows[0]["manager_name"] == "", (
        "matched across different leagues — manager names would be attributed "
        "to strangers"
    )


@pytest.mark.asyncio
async def test_a_different_team_slot_does_not_match():
    """Same league, different team number — must stay blank."""
    rows = await _store(
        [_pick("461.l.78512.t.3")],
        {"470.l.78512.t.9": "The Lord"},
    )
    assert rows[0]["manager_name"] == ""


@pytest.mark.asyncio
async def test_an_unknown_team_id_stays_blank_rather_than_guessing():
    """A miss must not fall through to some other manager's name."""
    rows = await _store([_pick("99")], {"3": "Ben Dover"})
    assert rows[0]["manager_name"] == ""


@pytest.mark.asyncio
async def test_a_bare_index_does_not_match_a_yahoo_team_key():
    """The mismatch that would mis-attribute names.

    If a future refactor keys the map on the bare team index while Yahoo picks
    carry the full team_key (or vice versa), this must stay blank rather than
    silently binding to the wrong manager.
    """
    rows = await _store([_pick("461.l.78512.t.9")], {"9": "Wrong Person"})
    assert rows[0]["manager_name"] == "", (
        "a bare index matched a full Yahoo team_key — names would be "
        "attributed to the wrong manager"
    )


@pytest.mark.asyncio
async def test_adapter_supplied_name_wins_over_the_map():
    """If an adapter ever starts sending a real name, it must not be overridden."""
    p = _pick("3")
    p.manager_name = "From Adapter"
    rows = await _store([p], {"3": "From Map"})
    assert rows[0]["manager_name"] == "From Adapter"


@pytest.mark.asyncio
async def test_no_manager_map_is_tolerated():
    """A league whose roster fetch produced nothing must still store picks."""
    rows = await _store([_pick("3")], None)
    assert rows[0]["manager_name"] == ""
