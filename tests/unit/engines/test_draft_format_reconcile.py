"""Symmetric live-format reconciliation — a stale session inside the resume
window must adopt the LIVE draft's format in BOTH directions, or it mis-routes
recommendations (snake session on an auction draft, and vice versa)."""
from backend.engines.draft_state_manager import DraftStateManager, LeagueConfig


def test_snake_session_reconciles_to_auction_and_restores_budget():
    # The reported case: a stale snake session (budget zeroed) on an auction draft.
    s = DraftStateManager(LeagueConfig(draft_type="snake", auction_budget=0))
    assert s.is_snake and s.your_budget == 0
    assert s.reconcile_draft_type("auction") is True
    assert s.is_auction
    assert s.your_budget == 200  # budget restored so the auction rec has one


def test_auction_session_reconciles_to_snake_symmetric():
    # The equally-real symmetric case: a stale auction session on a snake draft.
    a = DraftStateManager(LeagueConfig(draft_type="auction", auction_budget=200))
    assert a.is_auction
    assert a.reconcile_draft_type("snake") is True
    assert a.is_snake


def test_reconcile_is_noop_when_format_already_matches():
    a = DraftStateManager(LeagueConfig(draft_type="auction"))
    assert a.reconcile_draft_type("auction") is False
    s = DraftStateManager(LeagueConfig(draft_type="snake"))
    assert s.reconcile_draft_type("snake") is False


def test_reconcile_ignores_unknown_format():
    a = DraftStateManager(LeagueConfig(draft_type="auction"))
    assert a.reconcile_draft_type("linear") is False
    assert a.is_auction  # unchanged


def test_reconcile_to_auction_keeps_existing_positive_budget():
    # If the session already carries a real auction budget, don't clobber it.
    s = DraftStateManager(LeagueConfig(draft_type="snake", auction_budget=150))
    s.reconcile_draft_type("auction")
    assert s.league_config.auction_budget == 150
