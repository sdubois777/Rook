"""DraftStateManager snapshot serialization — to_dict/from_dict round-trip.

Durability foundation: a mid-draft state must survive a process restart by being
snapshotted to the DB and rebuilt identically. Dict round-trip equality here is
necessary; the rehydration-CORRECTNESS test (identical /recommendation output
after evict+rehydrate) lives in the draft-session tests.
"""
from __future__ import annotations

import json

from backend.engines.draft_state_manager import (
    DraftPick,
    DraftStateManager,
    LeagueConfig,
)


def _mid_auction_state() -> DraftStateManager:
    """A realistic mid-auction state: several picks, partial rosters/budgets."""
    state = DraftStateManager(LeagueConfig(auction_budget=200, team_count=12), "my_team")
    # Your picks
    state.record_pick(DraftPick("nfl_1", "my_team", 54, "Bijan Robinson", "RB", 1))
    state.record_pick(DraftPick("nfl_2", "my_team", 7, "Sam LaPorta", "TE", 2))
    # Opponent picks (two opponents)
    state.record_pick(DraftPick("nfl_3", "opp_a", 61, "Ja'Marr Chase", "WR", 1))
    state.record_pick(DraftPick("nfl_4", "opp_b", 3, "Jordan Love", "QB", 4))
    state.record_pick(DraftPick("nfl_5", "opp_a", 28, "Mike Evans", "WR", 2))
    # Your last bid (unattributed-sale recovery state)
    state.record_my_bid("nfl_9", 22)
    # Snake-style drafted-name tracking + your snake picks
    state.record_snake_pick("Christian McCaffrey", "RB", 1, 1, is_yours=True)
    state.record_snake_pick("Tyreek Hill", "WR", 2, 1, is_yours=False)
    return state


def test_to_dict_is_json_serializable():
    snap = _mid_auction_state().to_dict()
    # Must survive a real JSON round-trip (it goes into a JSONB column).
    assert json.loads(json.dumps(snap)) == snap


def test_round_trip_preserves_budget_and_rosters():
    original = _mid_auction_state()
    restored = DraftStateManager.from_dict(original.to_dict())

    assert restored.your_budget == original.your_budget
    assert restored.your_team_id == original.your_team_id
    assert [p.player_id for p in restored.your_roster] == [
        p.player_id for p in original.your_roster
    ]
    assert restored.opponent_budgets == original.opponent_budgets
    assert {
        tid: [p.player_id for p in r] for tid, r in restored.opponent_rosters.items()
    } == {tid: [p.player_id for p in r] for tid, r in original.opponent_rosters.items()}
    assert len(restored.picks) == len(original.picks)


def test_round_trip_preserves_drafted_names_and_my_bid():
    original = _mid_auction_state()
    restored = DraftStateManager.from_dict(original.to_dict())

    # Drafted-name membership drives recommendation exclusion (snake).
    assert restored.is_drafted("Christian McCaffrey")
    assert restored.is_drafted("Tyreek Hill")
    assert restored.get_my_roster() == original.get_my_roster()
    # Unattributed-sale recovery state survives.
    assert restored.is_my_winning_bid("nfl_9", 22)


def test_round_trip_preserves_league_config():
    original = _mid_auction_state()
    restored = DraftStateManager.from_dict(original.to_dict())
    assert restored.league_config.team_count == 12
    assert restored.league_config.auction_budget == 200
    assert restored.league_config.draft_type == original.league_config.draft_type
    assert restored.league_config.total_roster_size == original.league_config.total_roster_size


def test_derived_budget_methods_match_after_round_trip():
    original = _mid_auction_state()
    restored = DraftStateManager.from_dict(original.to_dict())
    # The numbers the engine reads for recommendations must be identical.
    assert restored.get_your_remaining_budget() == original.get_your_remaining_budget()
    assert restored.get_spendable_on_this_player() == original.get_spendable_on_this_player()
    assert restored.get_roster_slots_remaining() == original.get_roster_slots_remaining()
    assert restored.get_your_positional_counts() == original.get_your_positional_counts()


def test_empty_state_round_trips():
    state = DraftStateManager(LeagueConfig(), "")
    restored = DraftStateManager.from_dict(state.to_dict())
    assert restored.your_budget == state.your_budget
    assert restored.picks == []
    assert restored.get_my_roster() == []
