"""
Tests for the canonical player resolver — the ONE place the #217 name-collision
guard lives (backend/utils/player_resolver.py) + the repo orchestrator's ID-first
priority and deterministic DST path (PlayerRepository.resolve_player).

The pure guard is exhaustively covered (this is the load-bearing #217 coverage);
resolve_player's id-priority + DST routing + name-collision-refusal are covered
with a fake session (no DB needed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from backend.repositories.player_repo import PlayerRepository
from backend.utils.player_resolver import guarded_name_pick, name_match_tier


@dataclass
class _P:
    """A Player-shaped stand-in for the pure guard."""
    id: str
    name: str
    position: str = "WR"
    team_abbr: Optional[str] = None
    sleeper_id: Optional[str] = None
    tier: Optional[int] = None
    recommended_bid_ceiling: Optional[float] = None
    ai_bid_ceiling: Optional[int] = None


# ---------------------------------------------------------------------------
# name_match_tier — the #217 discriminator
# ---------------------------------------------------------------------------
def test_tier0_suffix_normalized_full_equal():
    assert name_match_tier("Chris Godwin", "Chris Godwin Jr.") == 0


def test_tier1_first_initial_plus_full_last():
    assert name_match_tier("M. Evans", "Mike Evans") == 1


def test_tier2_last_name_only_disagreeing_first():
    # The #217 class: same surname, different person.
    assert name_match_tier("Mike Evans", "Omari Evans") == 2
    assert name_match_tier("Chris Godwin", "Terry Godwin") == 2


def test_tier2_guards_multi_token_last_name_collision():
    # "A. Brown" must NOT match "Amon-Ra St. Brown" (both end in brown).
    assert name_match_tier("A. Brown", "Amon-Ra St. Brown") == 2
    # ...while a true match still lands.
    assert name_match_tier("A.J. Brown", "A.J. Brown") == 0


# ---------------------------------------------------------------------------
# guarded_name_pick — refuse collisions, never candidates[0]
# ---------------------------------------------------------------------------
def test_pick_refuses_last_name_only_collision():
    # Two same-surname players, query is a BARE surname → no first-name agreement.
    cands = [_P("1", "Mike Evans", tier=1), _P("2", "Omari Evans", tier=5)]
    assert guarded_name_pick(cands, "Evans") is None       # refused, not candidates[0]


def test_pick_selects_first_name_agreeing_over_prominence():
    # Even though Chase Brown is more prominent, "A.J. Brown" must resolve to A.J.
    cands = [_P("chase", "Chase Brown", tier=1, ai_bid_ceiling=40),
             _P("aj", "A.J. Brown", tier=1, ai_bid_ceiling=55)]
    assert guarded_name_pick(cands, "Chase Brown").id == "chase"
    assert guarded_name_pick(cands, "A.J. Brown").id == "aj"


def test_pick_position_filter_is_a_hard_guard():
    cands = [_P("wr", "Josh Allen", position="WR"), _P("qb", "Josh Allen", position="QB")]
    assert guarded_name_pick(cands, "Josh Allen", position="QB").id == "qb"


def test_pick_prominence_prefers_sleeper_then_tier():
    cands = [_P("stale", "Mike Evans", sleeper_id=None, tier=None),
             _P("live", "Mike Evans", sleeper_id="123", tier=1)]
    assert guarded_name_pick(cands, "Mike Evans").id == "live"


def test_pick_empty_and_no_name():
    assert guarded_name_pick([], "Mike Evans") is None
    assert guarded_name_pick([_P("1", "Mike Evans")], None) is None


# ---------------------------------------------------------------------------
# resolve_player — ID-first priority + deterministic DST (fake session)
# ---------------------------------------------------------------------------
class _Res:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj

    def scalars(self):
        rows = self._obj if isinstance(self._obj, list) else []
        return type("S", (), {"all": lambda _s: rows})()


class _FakeSession:
    """Returns queued results in call order; records how many queries ran."""
    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, _stmt):
        obj = self._results[self.calls] if self.calls < len(self._results) else None
        self.calls += 1
        return _Res(obj)


def _repo(results):
    r = PlayerRepository.__new__(PlayerRepository)
    r._session = _FakeSession(results)
    return r


async def test_resolve_player_id_first_ignores_name():
    """An espn_id hit resolves deterministically — the (wrong) name is never used,
    and only ONE query runs (sleeper/sportradar/gsis are skipped when unset)."""
    hit = _P("espn-hit", "Correct Player")
    repo = _repo([hit])
    got = await repo.resolve_player(espn_id="4360174", name="COMPLETELY WRONG NAME")
    assert got.id == "espn-hit"
    assert repo._session.calls == 1          # only the espn lookup fired


async def test_resolve_player_falls_to_guarded_name_and_refuses_collision():
    """No id → name fallback → the shared guard refuses a bare-surname collision."""
    cands = [_P("1", "Mike Evans"), _P("2", "Omari Evans")]
    repo = _repo([cands])                     # the name-candidates query
    assert await repo.resolve_player(name="Evans") is None


async def test_resolve_player_dst_routes_to_team_map_never_name():
    """position=DEF routes to the team map (one exact query), never name-fuzzy."""
    den = _P("den", "Denver Broncos", position="DEF", team_abbr="DEN")
    repo = _repo([den])
    got = await repo.resolve_player(position="DEF", team="DEN", name="ignored")
    assert got.id == "den"
    assert repo._session.calls == 1          # single deterministic DST query
