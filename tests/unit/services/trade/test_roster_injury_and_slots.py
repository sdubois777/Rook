"""Which injury evidence wins, and where the manager has each player seated.

Both of these were hardcoded in resolve_team_rosters: the lineup slot to nothing, and
the injury to a platform field that no reader ever filled. So on every real league the
injury was absent for 100% of rostered players, which silently disabled the filters
that keep an unavailable player out of the optimal lineup. The most expensive symptom
is on the waiver recommendation, which charges 2 credits before doing any work and has
no refund path: the baseline lineup an add has to beat was inflated by the full weekly
points of a player who cannot play, so the add that would actually fill the hole scored
near zero and the customer was told nothing on waivers cracks their lineup.

The precedence below is deliberate. A manager's own injured-reserve placement is the
strongest evidence there is — it is that league's actual state, and no league-wide
injury feed can express it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from backend.integrations.platform_models import RosteredPlayer, TeamRoster
from backend.services.trade.real_league_source import (
    INJURY_STALE_AFTER,
    _resolve_injury,
    resolve_team_rosters,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _player(injury=None, age=None, name="Some Player"):
    """A canonical players row. `age` is how long ago its injury was last confirmed."""
    p = MagicMock()
    p.name = name
    p.injury_status = injury
    p.injury_status_updated_at = None if age is None else NOW - age
    return p


def _rostered(slot=None, injury=None):
    return RosteredPlayer(platform_player_id="1", player_name="Some Player",
                          position="RB", team_abbr="SF",
                          lineup_slot=slot, injury_status=injury)


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------
def test_a_managers_own_injured_reserve_placement_wins():
    """The customer put this player on injured reserve in their league. That beats a
    stored record saying healthy, and it beats the platform calling them active."""
    healthy_everywhere = _player(injury=None, age=timedelta(hours=1))
    assert _resolve_injury(healthy_everywhere, _rostered(slot="IR"), NOW) == "IR"
    assert _resolve_injury(healthy_everywhere,
                           _rostered(slot="IR", injury="ACTIVE"), NOW) == "IR"


def test_the_platforms_own_designation_beats_the_stored_record():
    """The platform sent a designation with the roster, so it is current. The stored
    record refreshes weekly and can disagree."""
    stale_record = _player(injury="Q", age=timedelta(days=6))
    assert _resolve_injury(stale_record, _rostered(injury="OUT"), NOW) == "O"


def test_the_stored_record_is_used_when_the_platform_says_nothing():
    """Sleeper's league roster carries no injury field at all, so this is the only
    source for a Sleeper league."""
    assert _resolve_injury(_player(injury="Q", age=timedelta(days=2)),
                           _rostered(), NOW) == "Q"


def test_nothing_known_means_no_claim():
    assert _resolve_injury(_player(), _rostered(), NOW) is None


# ---------------------------------------------------------------------------
# Freshness. The stored record refreshes on a WEEKLY sweep, so about a week old is
# normal. The ceiling exists to catch that sweep having stopped.
# ---------------------------------------------------------------------------
def test_a_normally_aged_record_is_still_used():
    """A week old is ordinary operation, not a fault — withholding it would leave
    almost every league with no injury information at all."""
    assert _resolve_injury(_player(injury="O", age=timedelta(days=6)),
                           _rostered(), NOW) == "O"


def test_an_abandoned_record_is_withheld_rather_than_shown():
    """Past the ceiling the weekly refresh has evidently stopped. Showing a
    months-old injury is a confident wrong answer; showing nothing is honest."""
    ancient = _player(injury="O", age=INJURY_STALE_AFTER + timedelta(days=1))
    assert _resolve_injury(ancient, _rostered(), NOW) is None


def test_a_record_with_no_timestamp_is_withheld():
    """Without a timestamp we cannot say how old it is, so we do not stand behind it."""
    assert _resolve_injury(_player(injury="IR", age=None), _rostered(), NOW) is None


def test_an_injured_reserve_placement_is_used_even_with_an_abandoned_record():
    """The placement does not depend on the stored record's freshness at all."""
    ancient = _player(injury=None, age=INJURY_STALE_AFTER + timedelta(days=30))
    assert _resolve_injury(ancient, _rostered(slot="IR"), NOW) == "IR"


# ---------------------------------------------------------------------------
# Normalization of each platform's own spelling
# ---------------------------------------------------------------------------
def test_espn_spellings_are_normalized_not_passed_through():
    """ESPN writes SCREAMING_SNAKE. These must become the codes the lineup filters
    branch on, or an unavailable player is seated."""
    for raw, expected in (("QUESTIONABLE", "Q"), ("OUT", "O"),
                          ("INJURY_RESERVE", "IR"), ("ACTIVE", None)):
        assert _resolve_injury(_player(), _rostered(injury=raw), NOW) == expected


def test_an_unrecognized_spelling_produces_no_badge_rather_than_a_guess():
    assert _resolve_injury(_player(), _rostered(injury="SOME_NEW_STATUS"), NOW) is None


# ---------------------------------------------------------------------------
# End to end through resolve_team_rosters, which is what actually feeds the engine
# ---------------------------------------------------------------------------
class _FakePlayer:
    def __init__(self, pid, name, injury=None, age=None):
        self.id = pid
        self.name = name
        self.position = "RB"
        self.team_abbr = "SF"
        self.injury_status = injury
        self.injury_status_updated_at = None if age is None else NOW - age


async def test_resolve_team_rosters_carries_both_fields_through(monkeypatch):
    """The engine-facing shape must receive the slot and the injury. Before this,
    every player arrived with neither, so every real roster rendered as entirely
    bench and the injury-aware filters had nothing to act on."""
    table = {
        "starter": _FakePlayer("p1", "Starter"),
        "hurt": _FakePlayer("p2", "Hurt Guy"),
        "stored": _FakePlayer("p3", "Stored Guy", injury="Q", age=timedelta(days=2)),
    }

    async def _resolve(self, *, sleeper_id=None, espn_id=None, yahoo_id=None,
                       gsis_id=None, sportradar_id=None, name=None,
                       position=None, team=None):
        return table.get(sleeper_id)

    from backend.repositories.player_repo import PlayerRepository
    monkeypatch.setattr(PlayerRepository, "resolve_player", _resolve)

    tr = TeamRoster(platform_team_id="t1", manager_name="Me", team_name="My Team", players=[
        RosteredPlayer(platform_player_id="starter", player_name="Starter",
                       position="RB", team_abbr="SF", lineup_slot="RB"),
        RosteredPlayer(platform_player_id="hurt", player_name="Hurt Guy",
                       position="RB", team_abbr="SF", lineup_slot="IR"),
        RosteredPlayer(platform_player_id="stored", player_name="Stored Guy",
                       position="RB", team_abbr="SF", lineup_slot="BENCH"),
    ])

    teams, unresolved = await resolve_team_rosters(None, "sleeper", [tr], my_team_id="t1")

    by_name = {p.name: p for p in teams[0].roster}
    assert by_name["Starter"].starter_slot == "RB"
    assert by_name["Starter"].injury_status is None
    assert by_name["Hurt Guy"].starter_slot == "IR"
    assert by_name["Hurt Guy"].injury_status == "IR"
    assert by_name["Stored Guy"].starter_slot == "BENCH"
    assert by_name["Stored Guy"].injury_status == "Q"
    assert unresolved == []


async def test_an_unknown_slot_stays_unknown_through_to_the_engine(monkeypatch):
    """A platform that could not tell us where a player sits (Yahoo) must not have
    that turned into a claim on the way through."""
    async def _resolve(self, **kw):
        return _FakePlayer("p1", "Unknown Slot Guy")

    from backend.repositories.player_repo import PlayerRepository
    monkeypatch.setattr(PlayerRepository, "resolve_player", _resolve)

    tr = TeamRoster(platform_team_id="t1", manager_name="Me", team_name="My Team", players=[
        RosteredPlayer(platform_player_id="x", player_name="Unknown Slot Guy",
                       position="RB", team_abbr="SF"),      # no slot, no injury
    ])
    teams, _ = await resolve_team_rosters(None, "yahoo", [tr], my_team_id="t1")

    assert teams[0].roster[0].starter_slot is None
    assert teams[0].roster[0].injury_status is None
