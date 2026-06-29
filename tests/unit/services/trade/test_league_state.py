"""Unit tests for the league-state seam (backend/services/trade/league_state.py)."""
from __future__ import annotations

from backend.services.trade.league_state import (
    LeagueState,
    LeagueStateProvider,
    RosterPlayer,
    StaticLeagueStateProvider,
    TeamState,
)


def _player(pid: str, pos: str = "WR") -> RosterPlayer:
    return RosterPlayer(canonical_player_id=pid, name=f"P-{pid}", position=pos)


def _state() -> LeagueState:
    me = TeamState("t1", "Mine", is_me=True, roster=(_player("a"), _player("b", "RB")))
    opp = TeamState("t2", "Theirs", is_me=False, roster=(_player("c"),))
    return LeagueState(season=2025, week=5, teams=(me, opp))


def test_my_team_returns_the_is_me_team():
    assert _state().my_team.team_id == "t1"


def test_my_team_none_when_no_is_me():
    state = LeagueState(2025, 5, teams=(TeamState("x", "x", is_me=False),))
    assert state.my_team is None


def test_all_rostered_player_ids_spans_every_team():
    assert _state().all_rostered_player_ids() == {"a", "b", "c"}


def test_week_and_season_are_explicit_not_hardcoded():
    """The anchor is a field — a provider can serve any week/season."""
    state = LeagueState(season=2024, week=12, teams=())
    assert (state.season, state.week) == (2024, 12)


def test_static_provider_satisfies_protocol_and_returns_state():
    provider = StaticLeagueStateProvider(_state())
    assert isinstance(provider, LeagueStateProvider)
    assert provider.get_league_state().week == 5
