"""Idempotent pick recording — re-relayed picks (extension reload full-board
re-scan, page refresh, state backfill) must never double-charge budgets or
duplicate rosters. A player can only be drafted/sold once per draft."""
from backend.engines.draft_state_manager import (
    DraftPick,
    DraftStateManager,
    LeagueConfig,
)


def _mgr():
    return DraftStateManager(LeagueConfig(auction_budget=200))


# ── auction: record_pick ────────────────────────────────────────────────

def test_record_pick_dedupes_by_player_id():
    m = _mgr()
    assert m.record_pick(DraftPick(player_id="p1", team_id="Team 3", price=40, player_name="Josh Allen")) is True
    assert m.record_pick(DraftPick(player_id="p1", team_id="Team 3", price=40, player_name="Josh Allen")) is False
    assert len(m.picks) == 1
    assert m.opponent_budgets["Team 3"] == 160  # charged ONCE


def test_record_pick_dedupes_by_name_when_ids_missing():
    m = _mgr()
    assert m.record_pick(DraftPick(player_id="", team_id="Team 3", price=25, player_name="D.J. Moore")) is True
    # Name-variant re-relay (enrichment differences) is still the same sale.
    assert m.record_pick(DraftPick(player_id="", team_id="Team 3", price=25, player_name="DJ Moore")) is False
    assert len(m.picks) == 1


def test_record_pick_your_budget_charged_once():
    m = _mgr()
    m.record_pick(DraftPick(player_id="p9", team_id="You", price=55, player_name="CeeDee Lamb"), is_yours=True)
    m.record_pick(DraftPick(player_id="p9", team_id="You", price=55, player_name="CeeDee Lamb"), is_yours=True)
    assert m.your_budget == 145
    assert len(m.your_roster) == 1


def test_record_pick_different_players_both_record():
    m = _mgr()
    assert m.record_pick(DraftPick(player_id="p1", team_id="Team 1", price=10, player_name="A One")) is True
    assert m.record_pick(DraftPick(player_id="p2", team_id="Team 2", price=12, player_name="B Two")) is True
    assert len(m.picks) == 2


# ── snake: record_snake_pick ────────────────────────────────────────────

def test_record_snake_pick_dedupes_your_roster():
    m = _mgr()
    assert m.record_snake_pick("Jahmyr Gibbs", position="RB", pick_number=4, is_yours=True) is True
    assert m.record_snake_pick("Jahmyr Gibbs", position="RB", pick_number=4, is_yours=True) is False
    assert len(m.get_my_roster()) == 1
    assert m.is_drafted("Jahmyr Gibbs")


def test_record_snake_pick_dedupes_abbreviated_variant():
    m = _mgr()
    m.record_snake_pick("J. Gibbs", pick_number=4)  # DOM-abbreviated first
    assert m.record_snake_pick("Jahmyr Gibbs", pick_number=4) is False  # backfill full name
    assert m.is_drafted("Jahmyr Gibbs")


def test_record_snake_pick_empty_name_records_nothing():
    m = _mgr()
    assert m.record_snake_pick("", pick_number=1) is False
    assert m.get_my_roster() == []
