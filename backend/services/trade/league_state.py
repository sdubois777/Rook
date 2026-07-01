"""
League-state interface — the permanent seam between *where roster data comes
from* and *the value engine / trade agents that reason over it*.

One interface, swappable implementations:
  * (later, slice 2, test-only) a demo source that fabricates 12 rosters and
    serves 2025 weeks 1-5 as if "current";
  * (later, permanent) a real source built from SeasonRoster + live league sync.

The value engine and the trade agents depend ONLY on the dataclasses + the
``LeagueStateProvider`` protocol here — never on the data's origin. This slice
ships the seam plus a minimal static implementation sufficient to unit-test the
engine. It is deliberately NOT a seeder or a demo source.

Week-agnostic: the "current week" is an explicit field on ``LeagueState``. There
is no hardcoded week or season anywhere in this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class RosterPlayer:
    """One rostered player, identified by the Rook canonical UUID (the id the
    per-week data layer resolves to). Production/usage is NOT carried here — the
    value engine derives that from the per-week layer and returns it separately,
    keyed by ``canonical_player_id``.
    """
    canonical_player_id: str
    name: str
    position: str
    nfl_team: Optional[str] = None
    bye_week: Optional[int] = None
    starter_slot: Optional[str] = None  # e.g. "RB1", "FLEX", "BENCH" — optional


@dataclass(frozen=True)
class TeamState:
    team_id: str
    team_name: str
    is_me: bool
    roster: tuple[RosterPlayer, ...] = ()


@dataclass(frozen=True)
class LeagueState:
    """A point-in-time snapshot of a league: the current scoring-week anchor and
    every team's roster as canonical ids + positions.
    """
    season: int
    week: int
    teams: tuple[TeamState, ...] = ()

    @property
    def my_team(self) -> Optional[TeamState]:
        for team in self.teams:
            if team.is_me:
                return team
        return None

    def all_rostered_player_ids(self) -> set[str]:
        return {
            p.canonical_player_id for team in self.teams for p in team.roster
        }


@runtime_checkable
class LeagueStateProvider(Protocol):
    """The seam. Any source (demo or real) implements this single method."""

    def get_league_state(self) -> LeagueState:
        ...


@dataclass
class StaticLeagueStateProvider:
    """Minimal provider that returns a pre-built ``LeagueState`` verbatim.

    Enough to unit-test the value engine against fixed fixtures. It is a plain
    holder — NOT the demo seeder/source (that is slice 2, flag-gated).
    """

    state: LeagueState

    def get_league_state(self) -> LeagueState:
        return self.state
