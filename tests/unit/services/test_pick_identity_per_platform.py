"""Draft picks must resolve to real players on ESPN and Sleeper, not just Yahoo.

`_resolve_pick_identities` in backend/services/league_sync.py (the function that
maps a draft pick to a row in the players table) used to filter candidate keys
with `if ".p." in key`. Only Yahoo produces keys in that shape ("461.p.33963");
ESPN sends a bare integer and Sleeper a bare string. So the set of ids was always
empty for those two platforms, the function returned {} before running any query,
and every ESPN and Sleeper pick was stored with player_id = None.

ESPN was worse than Sleeper: its adapter also sends player_name="" and
position="" (backend/integrations/espn_league_api.py), so an ESPN pick row landed
with no id, no name and no position — unusable by every consumer.

The players table already carries espn_id at 89.4% coverage on players that have
a draft value, so this was a lookup that was never attempted, not missing data.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.league_sync import LeagueSyncService


def _pick(platform_player_id, name="", position=""):
    return SimpleNamespace(
        platform_player_id=platform_player_id,
        player_name=name,
        position=position,
        auction_price=10,
        manager_name="",
        picked_by_team_id="1",
        pick_number=1,
    )


def _svc_returning(rows):
    """Service whose DB returns `rows` and records each compiled statement."""
    db = MagicMock()
    stmts: list = []

    async def execute(stmt, *a, **kw):
        stmts.append(stmt)
        res = MagicMock()
        res.all.return_value = rows
        res.rowcount = 1
        return res

    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    svc = LeagueSyncService(db, uuid.uuid4())
    svc._stmts = stmts
    return svc


def _row(pid, player_uuid, name, position):
    r = MagicMock()
    r.pid = pid
    r.id = player_uuid
    r.name = name
    r.position = position
    return r


# --- key extraction ---------------------------------------------------------

def test_yahoo_key_is_the_numeric_tail():
    assert LeagueSyncService._pick_key("yahoo", "461.p.33963") == "33963"


def test_espn_and_sleeper_keys_are_used_as_is():
    assert LeagueSyncService._pick_key("espn", "4362628") == "4362628"
    assert LeagueSyncService._pick_key("sleeper", "6794") == "6794"


def test_a_yahoo_key_without_the_marker_is_rejected():
    """Yahoo ids are only meaningful as the tail of "<game>.p.<id>". A bare value
    here means the adapter changed shape; matching it against Player.yahoo_id
    anyway could bind a price to the wrong player."""
    assert LeagueSyncService._pick_key("yahoo", "33963") == ""


# --- per-platform resolution ------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,raw_id,expected_key,column",
    [
        ("espn", "4362628", "4362628", "espn_id"),
        ("sleeper", "6794", "6794", "sleeper_id"),
        ("yahoo", "461.p.33963", "33963", "yahoo_id"),
    ],
)
async def test_each_platform_queries_its_own_id_column(
    platform, raw_id, expected_key, column,
):
    """The whole defect in one assertion: ESPN and Sleeper must produce a query
    at all, and it must be against their own id column."""
    puid = uuid.uuid4()
    svc = _svc_returning([_row(expected_key, puid, "Bijan Robinson", "RB")])

    out = await svc._resolve_pick_identities([_pick(raw_id)], platform)

    assert out == {expected_key: (puid, "Bijan Robinson", "RB")}
    compiled = str(svc._stmts[0].compile(compile_kwargs={"literal_binds": True}))
    assert f"players.{column}" in compiled, (
        f"{platform} resolution did not query players.{column}: {compiled[:200]}"
    )


@pytest.mark.asyncio
async def test_espn_picks_no_longer_resolve_to_nothing():
    """Before the fix this returned {} without issuing a query."""
    svc = _svc_returning([_row("4362628", uuid.uuid4(), "Bijan Robinson", "RB")])
    out = await svc._resolve_pick_identities([_pick("4362628")], "espn")
    assert out, "ESPN picks resolved to nothing — the lookup was never attempted"
    assert svc._stmts, "no query was issued for an ESPN draft"


@pytest.mark.asyncio
async def test_an_unknown_platform_resolves_nothing_and_does_not_query():
    """Fail closed. Guessing a column would bind prices to arbitrary players."""
    svc = _svc_returning([])
    out = await svc._resolve_pick_identities([_pick("123")], "fanduel")
    assert out == {}
    assert svc._stmts == []


@pytest.mark.asyncio
async def test_ambiguous_ids_are_dropped_rather_than_guessed():
    """Two player rows sharing one platform id must resolve to NEITHER.

    The players table really does contain duplicate id values — measured on the
    production dump: 21 duplicated espn_id values and 18 duplicated yahoo_id
    values. Binding an auction price to the wrong one of a pair is worse than
    leaving the pick unresolved.
    """
    svc = _svc_returning([
        _row("4362628", uuid.uuid4(), "Bijan Robinson", "RB"),
        _row("4362628", uuid.uuid4(), "Someone Else", "WR"),
    ])
    out = await svc._resolve_pick_identities([_pick("4362628")], "espn")
    assert out == {}, "an ambiguous id was bound to a guess"


@pytest.mark.asyncio
async def test_espn_picks_get_a_name_and_position_from_the_resolved_player():
    """ESPN sends player_name="" and position="", so the stored row is only
    usable if resolution supplies them."""
    puid = uuid.uuid4()
    svc = _svc_returning([_row("4362628", puid, "Bijan Robinson", "RB")])
    out = await svc._resolve_pick_identities([_pick("4362628")], "espn")
    pid, name, position = out["4362628"]
    assert (pid, name, position) == (puid, "Bijan Robinson", "RB")
