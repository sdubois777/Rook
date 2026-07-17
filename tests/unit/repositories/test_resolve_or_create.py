"""Tests for PlayerRepository.resolve_or_create + _apply_ingest_fields (the canonical
ingest dedup path). Pure-logic for the field union; an in-memory resolve_player fake
(same id-first → guarded name+pos priority) drives the resolve/create branches — so the
placeholder→real gsis mechanism (the dupe seam) is proven without a DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.models.player import Player
from backend.repositories.player_repo import PlayerRepository, _apply_ingest_fields
from backend.utils.player_resolver import guarded_name_pick


# --------------------------------------------------------------------------- union
class TestApplyIngestFields:
    def test_fills_and_refreshes_non_none(self):
        p = Player(name="X", position="RB")
        _apply_ingest_fields(p, {"team_abbr": "ATL", "age": 24, "sleeper_id": "s1"})
        assert p.team_abbr == "ATL" and p.age == 24 and p.sleeper_id == "s1"

    def test_none_never_blanks_existing(self):
        p = Player(name="X", position="RB", team_abbr="ATL", sleeper_id="s1")
        _apply_ingest_fields(p, {"team_abbr": None, "sleeper_id": None})
        assert p.team_abbr == "ATL" and p.sleeper_id == "s1"

    def test_team_alias_maps_to_team_abbr(self):
        p = Player(name="X", position="RB")
        _apply_ingest_fields(p, {"team": "BUF"})
        assert p.team_abbr == "BUF"

    def test_gsis_placeholder_upgrades_to_real(self):
        p = Player(name="X", position="RB", gsis_id="LOV121782")   # Sleeper placeholder
        _apply_ingest_fields(p, {"gsis_id": "00-0041027"})          # nflverse real
        assert p.gsis_id == "00-0041027"

    def test_gsis_real_not_downgraded_to_placeholder(self):
        p = Player(name="X", position="RB", gsis_id="00-0041027")   # real
        _apply_ingest_fields(p, {"gsis_id": "LOV121782"})           # Sleeper placeholder
        assert p.gsis_id == "00-0041027"                            # kept the real one

    def test_gsis_fills_when_empty(self):
        p = Player(name="X", position="RB", gsis_id=None)
        _apply_ingest_fields(p, {"gsis_id": "LOV121782"})
        assert p.gsis_id == "LOV121782"

    def test_yahoo_player_id_fill_if_empty_only(self):
        p = Player(name="X", position="RB", yahoo_player_id="nfl_LOV121782")
        _apply_ingest_fields(p, {"yahoo_player_id": "nfl_00-0041027"})
        assert p.yahoo_player_id == "nfl_LOV121782"                 # not overwritten (unique/derived)
        p2 = Player(name="Y", position="RB")
        _apply_ingest_fields(p2, {"yahoo_player_id": "nfl_00-1"})
        assert p2.yahoo_player_id == "nfl_00-1"                     # filled when empty

    def test_ignores_unknown_keys(self):
        p = Player(name="X", position="RB")
        _apply_ingest_fields(p, {"not_a_column": "z", "name": "X"})
        assert not hasattr(p, "not_a_column")


# ---------------------------------------------------- in-memory repo (fakes the DB find)
class _MemRepo(PlayerRepository):
    def __init__(self, players):
        self._players = players

        class _S:
            def add(inner, obj):
                players.append(obj)
        self._session = _S()

    async def resolve_player(self, *, sleeper_id=None, espn_id=None, yahoo_id=None,
                             gsis_id=None, sportradar_id=None, name=None,
                             position=None, team=None):
        for val, col in ((sleeper_id, "sleeper_id"), (sportradar_id, "sportradar_id"),
                         (gsis_id, "gsis_id"), (espn_id, "espn_id"), (yahoo_id, "yahoo_id")):
            v = (val or "").strip()
            if v:
                for p in self._players:
                    if str(getattr(p, col, None) or "") == v:
                        return p
        if not name:
            return None
        pos = (position or "").upper()
        last = name.split()[-1]
        cands = [p for p in self._players if p.name and last.lower() in p.name.lower()
                 and (not pos or (p.position or "").upper() == pos)]
        return guarded_name_pick(cands, name, team=team, position=position)


@pytest.mark.asyncio
async def test_placeholder_then_real_gsis_updates_not_inserts():
    """THE dupe seam: a Sleeper row (placeholder gsis + sleeper_id) then an nflverse row
    (real gsis, same human, NO sleeper_id) must resolve to ONE row via guarded name+pos —
    unioning the real gsis — not a second insert."""
    sleeper_row = Player(name="Jeremiyah Love", position="RB", team_abbr="ARI",
                         sleeper_id="13287", gsis_id="LOV121782")
    players = [sleeper_row]
    repo = _MemRepo(players)

    player, created = await repo.resolve_or_create({
        "name": "Jeremiyah Love", "position": "RB", "team_abbr": "ARI",
        "gsis_id": "00-0041027", "baseline_value": 4.81,   # nflverse row: real gsis, no sleeper_id
    })

    assert created is False                      # matched, did NOT insert
    assert len(players) == 1                     # still ONE row
    assert player is sleeper_row
    assert player.sleeper_id == "13287"          # kept from the Sleeper row
    assert player.gsis_id == "00-0041027"        # unioned the real gsis (placeholder upgraded)
    assert float(player.baseline_value) == 4.81  # unioned the valuation


@pytest.mark.asyncio
async def test_inserts_when_no_match():
    players = []
    repo = _MemRepo(players)
    player, created = await repo.resolve_or_create(
        {"name": "New Rookie", "position": "WR", "gsis_id": "00-9999"})
    assert created is True and len(players) == 1 and player.name == "New Rookie"


@pytest.mark.asyncio
async def test_allow_create_false_refuses_insert():
    players = []
    repo = _MemRepo(players)
    player, created = await repo.resolve_or_create(
        {"name": "Deep Guy", "position": "WR"}, allow_create=False)
    assert player is None and created is False and players == []


@pytest.mark.asyncio
async def test_allow_create_callable_only_evaluated_on_miss():
    hit = _MemRepo([Player(name="Star WR", position="WR", sleeper_id="9")])
    calls = {"n": 0}

    def gate():
        calls["n"] += 1
        return True

    await hit.resolve_or_create({"name": "Star WR", "position": "WR", "sleeper_id": "9"},
                                allow_create=gate)
    assert calls["n"] == 0   # matched → gate never evaluated


@pytest.mark.asyncio
async def test_on_update_fires_before_union():
    existing = Player(name="Mover", position="RB", team_abbr="ATL", sleeper_id="1")
    repo = _MemRepo([existing])
    seen = {}

    def hook(row, data):
        seen["old_team"] = row.team_abbr        # still ATL (pre-union)
        seen["new_team"] = data.get("team_abbr")

    await repo.resolve_or_create(
        {"name": "Mover", "position": "RB", "team_abbr": "BUF", "sleeper_id": "1"},
        on_update=hook)
    assert seen == {"old_team": "ATL", "new_team": "BUF"}
    assert existing.team_abbr == "BUF"          # union applied after the hook
