"""
tests/unit/engines/test_draft_state_manager.py

Direct unit tests for backend/engines/draft_state_manager.py.

Covers core state transitions: initial budgets/roster state, pick recording
for my team vs opponents, remaining budget math, spendable calculations,
positional counts, and config_from_user_league defaults/overrides.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.engines.draft_state_manager import (
    DraftPick,
    DraftStateManager,
    LeagueConfig,
)

MY_TEAM = "my_team"
OPP_TEAM = "opp_1"


def _make_state(budget: int = 200) -> DraftStateManager:
    return DraftStateManager(LeagueConfig(auction_budget=budget, min_bid=1), MY_TEAM)


def _pick(player_id: str, team_id: str, price: int, position: str = "RB") -> DraftPick:
    return DraftPick(player_id=player_id, team_id=team_id, price=price, position=position)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_initial_state_no_picks_full_budget_empty_rosters():
    """A fresh manager has the full budget, no picks, and empty rosters."""
    state = _make_state()

    assert state.get_your_remaining_budget() == 200
    assert state.picks == []
    assert state.your_roster == []
    assert state.opponent_rosters == {}
    assert state.opponent_budgets == {}
    assert state.get_drafted_player_ids() == set()


def test_total_roster_size_default_slots_sums_to_sixteen():
    """Default roster slots (QB/RB/WR/FLEX/TE/K/DEF/BENCH) sum to 16."""
    assert LeagueConfig().total_roster_size == 16


def test_roster_slots_remaining_no_picks_equals_total_roster_size():
    """With no picks recorded, all roster slots remain open."""
    state = _make_state()
    assert state.get_roster_slots_remaining() == 16


# ---------------------------------------------------------------------------
# Recording picks — my team vs opponents
# ---------------------------------------------------------------------------

def test_record_pick_my_team_reduces_budget_and_fills_roster():
    """A pick by my team deducts its price from my budget and joins my roster."""
    state = _make_state()

    state.record_pick(_pick("p1", MY_TEAM, 45))

    assert state.get_your_remaining_budget() == 155
    assert len(state.your_roster) == 1
    assert state.your_roster[0].player_id == "p1"
    assert state.opponent_rosters == {}


def test_record_pick_is_yours_routes_to_your_roster_despite_slot_label_team_id():
    """is_yours (the extension's own-pick flag) routes a pick to YOUR roster even
    when team_id is an anonymous slot label that doesn't match your_team_id — the
    Sleeper/ESPN case where own buys otherwise landed in opponent_rosters."""
    state = _make_state()

    state.record_pick(_pick("p1", "Team 5", 70), is_yours=True)

    assert len(state.your_roster) == 1
    assert state.your_roster[0].player_id == "p1"
    assert state.get_your_remaining_budget() == 130  # 200 − 70
    assert state.opponent_rosters == {}  # NOT a phantom "Team 5"


def test_record_pick_opponent_initializes_budget_and_tracks_roster():
    """An opponent's first pick seeds their budget at full and deducts the price."""
    state = _make_state()

    state.record_pick(_pick("p1", OPP_TEAM, 60))

    assert state.opponent_budgets[OPP_TEAM] == 140
    assert len(state.opponent_rosters[OPP_TEAM]) == 1
    # My budget and roster are untouched
    assert state.get_your_remaining_budget() == 200
    assert state.your_roster == []


def test_record_pick_opponent_second_pick_accumulates_spend():
    """Subsequent opponent picks keep deducting from their tracked budget."""
    state = _make_state()

    state.record_pick(_pick("p1", OPP_TEAM, 60))
    state.record_pick(_pick("p2", OPP_TEAM, 25))

    assert state.opponent_budgets[OPP_TEAM] == 115
    assert len(state.opponent_rosters[OPP_TEAM]) == 2


def test_record_pick_multiple_opponents_tracked_independently():
    """Each opponent team gets its own budget and roster bucket."""
    state = _make_state()

    state.record_pick(_pick("p1", "opp_1", 50))
    state.record_pick(_pick("p2", "opp_2", 30))

    assert state.opponent_budgets["opp_1"] == 150
    assert state.opponent_budgets["opp_2"] == 170
    assert len(state.opponent_rosters["opp_1"]) == 1
    assert len(state.opponent_rosters["opp_2"]) == 1


def test_get_drafted_player_ids_after_picks_contains_all_teams_players():
    """Drafted IDs include picks by my team and opponents alike."""
    state = _make_state()

    state.record_pick(_pick("mine", MY_TEAM, 40))
    state.record_pick(_pick("theirs", OPP_TEAM, 35))

    assert state.get_drafted_player_ids() == {"mine", "theirs"}


# ---------------------------------------------------------------------------
# Budget math
# ---------------------------------------------------------------------------

def test_roster_slots_remaining_after_my_picks_decrements():
    """Only my picks consume my roster slots — opponent picks do not."""
    state = _make_state()

    state.record_pick(_pick("p1", MY_TEAM, 10))
    state.record_pick(_pick("p2", MY_TEAM, 10))
    state.record_pick(_pick("p3", OPP_TEAM, 10))

    assert state.get_roster_slots_remaining() == 14


def test_minimum_completion_budget_one_dollar_per_open_slot():
    """Minimum completion budget is min_bid for every unfilled roster slot."""
    state = _make_state()
    state.record_pick(_pick("p1", MY_TEAM, 30))

    assert state.get_minimum_completion_budget() == 15  # 16 - 1 picks


def test_spendable_mid_draft_reserves_dollar_per_remaining_slot():
    """Spendable amount equals remaining budget minus $1 per open slot."""
    state = _make_state()
    for i in range(4):
        state.record_pick(_pick(f"p{i}", MY_TEAM, 30))  # spent 120

    # budget 80, slots remaining 12 → spendable 68
    assert state.get_spendable_on_this_player() == 68


def test_spendable_budget_exhausted_clamps_to_zero():
    """Spendable never goes negative even when budget cannot cover open slots."""
    state = _make_state()
    for i in range(5):
        state.record_pick(_pick(f"p{i}", MY_TEAM, 39))  # spent 195

    # budget 5, slots remaining 11, min completion 11 → max(0, -6) = 0
    assert state.get_spendable_on_this_player() == 0


def test_positional_counts_mixed_roster_counts_each_position():
    """Positional counts tally my roster by position string."""
    state = _make_state()
    state.record_pick(_pick("p1", MY_TEAM, 10, position="RB"))
    state.record_pick(_pick("p2", MY_TEAM, 10, position="RB"))
    state.record_pick(_pick("p3", MY_TEAM, 10, position="WR"))
    state.record_pick(_pick("p4", OPP_TEAM, 10, position="WR"))  # opponent — ignored

    assert state.get_your_positional_counts() == {"RB": 2, "WR": 1}


# ---------------------------------------------------------------------------
# config_from_user_league
# ---------------------------------------------------------------------------

def test_config_from_user_league_none_returns_defaults():
    """No connected league falls back to the default 200/12 auction config."""
    config = DraftStateManager.config_from_user_league(None)

    assert config.auction_budget == 200
    assert config.min_bid == 1
    assert config.team_count == 12


def test_config_from_user_league_auction_league_reflects_budget_and_teams():
    """A connected auction league's budget and team count flow into the config."""
    league = SimpleNamespace(budget=300, draft_type="auction", team_count=10)

    config = DraftStateManager.config_from_user_league(league)

    assert config.auction_budget == 300
    assert config.team_count == 10
    assert config.min_bid == 1


def test_config_from_user_league_snake_draft_zeroes_auction_budget():
    """Non-auction (snake) leagues get auction_budget=0."""
    league = SimpleNamespace(budget=250, draft_type="snake", team_count=12)

    config = DraftStateManager.config_from_user_league(league)

    assert config.auction_budget == 0


def test_config_from_user_league_null_fields_use_defaults():
    """League with missing budget/draft_type/team_count falls back to defaults."""
    league = SimpleNamespace(budget=None, draft_type=None, team_count=None)

    config = DraftStateManager.config_from_user_league(league)

    assert config.auction_budget == 200  # None draft_type defaults to auction
    assert config.team_count == 12


# ---------------------------------------------------------------------------
# my_bid tracking — recover an unattributed win (winner='unknown')
# ---------------------------------------------------------------------------

def test_record_my_bid_stores_player_and_amount():
    state = _make_state()
    state.record_my_bid("nfl.p.100", 42)
    assert state.last_my_bid == {"player_id": "nfl.p.100", "amount": 42}


def test_winner_unknown_fallback_via_my_bid_match():
    """A sale at your last bid price + matching player id is recognized as yours."""
    state = _make_state()
    state.record_my_bid("nfl.p.100", 42)

    # Same player, same price -> yours.
    assert state.is_my_winning_bid("nfl.p.100", 42) is True


def test_my_bid_match_falls_back_to_price_when_id_unknown():
    """When the sold player's id couldn't be resolved, price alone decides."""
    state = _make_state()
    state.record_my_bid("nfl.p.100", 42)

    assert state.is_my_winning_bid("", 42) is True


def test_my_bid_no_match_on_price_mismatch():
    """A different final price means someone outbid you — not yours."""
    state = _make_state()
    state.record_my_bid("nfl.p.100", 42)

    assert state.is_my_winning_bid("nfl.p.100", 50) is False


def test_my_bid_no_match_on_different_player():
    """Right price but a different known player id is not your win."""
    state = _make_state()
    state.record_my_bid("nfl.p.100", 42)

    assert state.is_my_winning_bid("nfl.p.999", 42) is False


def test_my_bid_no_match_when_never_bid():
    """No recorded bid -> never your win."""
    state = _make_state()
    assert state.is_my_winning_bid("nfl.p.100", 42) is False
