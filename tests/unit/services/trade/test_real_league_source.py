"""
Tests for the RealLeagueSource (services/trade/real_league_source.py) — deterministic
roster resolution via the canonical resolve_player (Yahoo key normalization + explicit
DST routing), the real league shape, and the derived FA pool (exclusion by canonical
id). resolve_player is monkeypatched so these run without a DB; the pure helpers +
_derive_pool use a fake session.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.integrations.platform_models import RosteredPlayer, TeamRoster
from backend.services.trade import real_league_source as rls
from backend.services.trade.real_league_source import (
    _derive_pool,
    _resolver_kwargs,
    _roster_limit_from_slots,
    build_real_league_source,
    normalize_yahoo_id,
    resolve_team_rosters,
)


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def test_normalize_yahoo_id_strips_game_prefix():
    assert normalize_yahoo_id("449.p.34158") == "34158"   # the real player_key shape
    assert normalize_yahoo_id("34158") == "34158"
    assert normalize_yahoo_id("") is None
    assert normalize_yahoo_id(None) is None


def test_resolver_kwargs_per_platform_id():
    def rp(pos="WR", pid="X", team="DEN"):
        return RosteredPlayer(platform_player_id=pid, player_name="A B", position=pos, team_abbr=team)

    assert _resolver_kwargs("sleeper", rp(pid="123"))["sleeper_id"] == "123"
    assert _resolver_kwargs("espn", rp(pid="456"))["espn_id"] == "456"
    # Yahoo: the raw key is NORMALIZED to the bare id.
    assert _resolver_kwargs("yahoo", rp(pid="449.p.789"))["yahoo_id"] == "789"
    # Every mapping carries name+position+team for the guarded fallback.
    k = _resolver_kwargs("espn", rp())
    assert k["name"] == "A B" and k["position"] == "WR" and k["team"] == "DEN"


def test_resolver_kwargs_dst_has_no_id_only_team():
    """A DST carries NO platform id — position=DEF routes by team, never id/fuzzy."""
    dst = RosteredPlayer(platform_player_id="16", player_name="Broncos D/ST", position="DEF", team_abbr="DEN")
    k = _resolver_kwargs("espn", dst)
    assert "espn_id" not in k and "sleeper_id" not in k and "yahoo_id" not in k
    assert k["position"] == "DEF" and k["team"] == "DEN"


def test_roster_limit_from_slots():
    assert _roster_limit_from_slots({"QB": 1, "RB": 2, "WR": 3, "K": 1, "DEF": 1, "BN": 6}) == 14
    assert _roster_limit_from_slots(None) == rls.DEFAULT_ROSTER_LIMIT
    assert _roster_limit_from_slots({}) == rls.DEFAULT_ROSTER_LIMIT


# ---------------------------------------------------------------------------
# resolution (resolve_player monkeypatched)
# ---------------------------------------------------------------------------
class _FakePlayer:
    def __init__(self, pid, name, position, team="NFL"):
        self.id = pid
        self.name = name
        self.position = position
        self.team_abbr = team


def _patch_resolver(monkeypatch, table):
    """table: {(id_field, value) or ('DST', team): _FakePlayer}. resolve_player looks
    up by whichever id kwarg is set (or DST by team). Unknown → None."""
    async def _fake(self, *, sleeper_id=None, espn_id=None, yahoo_id=None, gsis_id=None,
                    sportradar_id=None, name=None, position=None, team=None):
        if (position or "").upper() == "DEF":
            return table.get(("DST", (team or "").upper()))
        for field, val in (("sleeper", sleeper_id), ("espn", espn_id), ("yahoo", yahoo_id)):
            if val and (field, val) in table:
                return table[(field, val)]
        return None
    from backend.repositories.player_repo import PlayerRepository
    monkeypatch.setattr(PlayerRepository, "resolve_player", _fake)


async def test_resolve_team_rosters_deterministic_ids_and_is_me(monkeypatch):
    table = {
        ("espn", "456"): _FakePlayer("p1", "Malik Nabers", "WR", "NYG"),
        ("DST", "DEN"): _FakePlayer("pden", "Denver Broncos", "DEF", "DEN"),
    }
    _patch_resolver(monkeypatch, table)
    tr = TeamRoster(platform_team_id="t1", manager_name="Me", team_name="My Team", players=[
        RosteredPlayer(platform_player_id="456", player_name="Malik Nabers", position="WR", team_abbr="NYG"),
        RosteredPlayer(platform_player_id="16", player_name="Broncos D/ST", position="DEF", team_abbr="DEN"),
    ])
    teams, unresolved = await resolve_team_rosters(None, "espn", [tr], my_team_id="t1")
    assert len(teams) == 1 and teams[0].is_me is True
    names = [rp.name for rp in teams[0].roster]
    assert names == ["Malik Nabers", "Denver Broncos"]      # DST routed by team, not the wrong name
    assert unresolved == []


async def test_resolve_team_rosters_yahoo_key_normalized(monkeypatch):
    table = {("yahoo", "34158"): _FakePlayer("p2", "James Cook", "RB", "BUF")}
    _patch_resolver(monkeypatch, table)
    tr = TeamRoster(platform_team_id="t2", manager_name="Opp", team_name="Opp", players=[
        RosteredPlayer(platform_player_id="449.p.34158", player_name="James Cook", position="RB", team_abbr="BUF"),
    ])
    teams, unresolved = await resolve_team_rosters(None, "yahoo", [tr], my_team_id=None)
    assert [rp.name for rp in teams[0].roster] == ["James Cook"]   # id hit via normalized key
    assert teams[0].is_me is False and unresolved == []


async def test_resolve_team_rosters_unresolved_is_loud_warned_not_silent(monkeypatch, caplog):
    _patch_resolver(monkeypatch, {})   # nothing resolves
    tr = TeamRoster(platform_team_id="t3", manager_name="X", team_name="X", players=[
        RosteredPlayer(platform_player_id="999", player_name="Nobody Fringe", position="WR", team_abbr="FA"),
    ])
    with caplog.at_level("WARNING"):
        teams, unresolved = await resolve_team_rosters(None, "espn", [tr], my_team_id=None)
    assert teams[0].roster == ()                     # dropped, not silently mis-resolved
    assert len(unresolved) == 1 and unresolved[0]["name"] == "Nobody Fringe"
    assert any("UNRESOLVED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# FA pool derivation (fake session)
# ---------------------------------------------------------------------------
class _Res:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _Res(self._rows)


async def test_derive_pool_excludes_rostered_by_canonical_id_and_includes_kdef():
    # weekly: 3 players with >=3 games (fa1, fa2 free; ros1 rostered) + 1 with <3 games.
    weekly = pd.DataFrame(
        [{"canonical_player_id": pid, "week": w} for pid in ("fa1", "fa2", "ros1") for w in (1, 2, 3)]
        + [{"canonical_player_id": "thin", "week": 1}]
    )
    # DB returns Player rows for the surviving candidate ids (fa1 WR, fa2 DEF).
    import uuid as _uuid
    fa1, fa2 = _uuid.uuid4(), _uuid.uuid4()
    # rebuild weekly with real uuids so _derive_pool's UUID() parse keeps them
    weekly = pd.DataFrame(
        [{"canonical_player_id": str(pid), "week": w} for pid in (fa1, fa2) for w in (1, 2, 3)]
        + [{"canonical_player_id": "ros1", "week": w} for w in (1, 2, 3)]
    )
    rows = [(fa1, "Free WR", "WR", "NYG", None), (fa2, "Free DEF", "DEF", "DEN", None)]
    pool = await _derive_pool(_FakeDB(rows), weekly, rostered_ids={"ros1"})
    ids = {rp.canonical_player_id for rp in pool}
    assert str(fa1) in ids and str(fa2) in ids      # free agents included (incl. DEF — streamed)
    assert "ros1" not in ids                          # rostered EXCLUDED by canonical id


# ---------------------------------------------------------------------------
# build assembly (week / slots / limit / is_me) — resolver + DB bits patched
# ---------------------------------------------------------------------------
async def test_build_real_league_source_assembles_real_shape(monkeypatch):
    _patch_resolver(monkeypatch, {("espn", "456"): _FakePlayer("p1", "Malik Nabers", "WR", "NYG")})
    monkeypatch.setattr(rls, "get_current_nfl_week", lambda season: 7)

    async def _no_pool(db, weekly, rostered):
        return []
    monkeypatch.setattr(rls, "_derive_pool", _no_pool)

    async def _no_priors(db, ids, scoring_format="ppr"):
        return {}
    monkeypatch.setattr("backend.services.trade.trade_demo_source._load_priors", _no_priors)

    class _League:
        platform = "espn"
        season_year = 2025
        roster_slots = {"QB": 1, "RB": 2, "WR": 4, "SUPER_FLEX": 1, "K": 1, "DEF": 1, "BN": 6}
        id = "L1"

    class _User:
        id = "u1"

    tr = TeamRoster(platform_team_id="t1", manager_name="Me", team_name="My Team", players=[
        RosteredPlayer(platform_player_id="456", player_name="Malik Nabers", position="WR", team_abbr="NYG"),
    ])
    src = await build_real_league_source(
        None, _User(), user_league=_League(),
        team_rosters=[tr], weekly_usage=pd.DataFrame(), my_team_id="t1",
    )
    st = src.get_league_state()
    assert st.week == 7 and st.season == 2025
    assert st.roster_slots == _League.roster_slots          # REAL shape, not default
    assert src.roster_limit == 16                            # sum of the slots
    assert st.my_team is not None and st.my_team.team_id == "t1"
    assert [rp.name for rp in st.my_team.roster] == ["Malik Nabers"]
