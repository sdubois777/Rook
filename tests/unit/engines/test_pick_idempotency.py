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


# ── two-emitter convergence (WS player_picked  +  REST gap reconciliation) ──
# The gap fix means a pick can now arrive from BOTH the live WS snake_pick and the
# REST backfill. Both resolve identity via the same sleeper_id → canonical Player,
# so both carry the same canonical name; the engine dedupes on that name in either
# order, with no double-count.

def test_two_emitter_ws_then_rest_no_double():
    m = _mgr()
    # WS player_picked landed live for this pick.
    assert m.record_snake_pick("Jahmyr Gibbs", position="RB", pick_number=4, is_yours=True) is True
    # REST reconciliation re-emits the same pick (same canonical name) — no-op.
    assert m.record_snake_pick("Jahmyr Gibbs", position="RB", pick_number=4, is_yours=True) is False
    assert len(m.get_my_roster()) == 1


def test_two_emitter_rest_then_late_ws_no_double():
    m = _mgr()
    # REST recovered a gap pick first (no player_picked was ever sent for it)...
    assert m.record_snake_pick("Bijan Robinson", position="RB", pick_number=3, is_yours=True) is True
    # ...then a LATE/duplicate WS frame for the same pick_no arrives — still deduped.
    assert m.record_snake_pick("Bijan Robinson", position="RB", pick_number=3, is_yours=True) is False
    assert len(m.get_my_roster()) == 1
    assert m.is_drafted("Bijan Robinson")


def test_two_emitter_opponent_pick_dedupes_by_name():
    m = _mgr()
    # An opponent gap pick recovered via REST, then a duplicate emit — drafted-set
    # dedup holds (exclusion list must not grow / double-count).
    assert m.record_snake_pick("Ja'Marr Chase", position="WR", pick_number=1) is True
    assert m.record_snake_pick("Ja'Marr Chase", position="WR", pick_number=1) is False
    assert m.is_drafted("Ja'Marr Chase")
    assert m.get_my_roster() == []  # not mine
