"""Storing each league's waiver settings on the league record during sync.

The one thing that is easy to get wrong here and expensive to get wrong: the
"does this league bid?" answer is THREE-state, and False is a real answer meaning
"this league claims by priority". A truthiness test (`if meta.uses_bidding_budget:`)
throws that answer away and leaves the league indistinguishable from one we never
managed to read — which puts it straight back on the fabricated-budget path.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from backend.integrations.platform_models import LeagueMetadata
from backend.services.league_sync import LeagueSyncService


def _service():
    return LeagueSyncService(AsyncMock(), uuid.uuid4())


def _blank_league():
    """A league record with the waiver fields unset, as a fresh row would be."""
    lg = MagicMock()
    lg.uses_bidding_budget = None
    lg.waiver_budget = None
    lg.waiver_type = None
    return lg


# ---------------------------------------------------------------------------
# ESPN / Sleeper — via LeagueMetadata
# ---------------------------------------------------------------------------
def test_false_is_stored_not_discarded():
    """A league that does NOT bid must be recorded as such. If this stores None
    instead, the waiver page falls back to assuming a budget for a league that
    never bids — the exact defect this work removes."""
    lg = _blank_league()
    _service()._apply_league_metadata(lg, LeagueMetadata(
        uses_bidding_budget=False, waiver_system="rolling priority"))

    assert lg.uses_bidding_budget is False
    assert lg.waiver_type == "rolling priority"
    assert lg.waiver_budget is None


def test_true_and_budget_are_stored():
    lg = _blank_league()
    _service()._apply_league_metadata(lg, LeagueMetadata(
        uses_bidding_budget=True, waiver_budget=250, waiver_system="budget"))

    assert lg.uses_bidding_budget is True
    assert lg.waiver_budget == 250
    assert lg.waiver_type == "budget"


def test_unknown_never_overwrites_a_known_answer():
    """A later sync that could not read the settings must leave the stored answer
    alone rather than resetting a known league to unknown."""
    lg = _blank_league()
    lg.uses_bidding_budget = True
    lg.waiver_budget = 200
    lg.waiver_type = "budget"

    _service()._apply_league_metadata(lg, LeagueMetadata())

    assert lg.uses_bidding_budget is True
    assert lg.waiver_budget == 200
    assert lg.waiver_type == "budget"


# ---------------------------------------------------------------------------
# Yahoo — a separate sync path that does not use LeagueMetadata at all
# ---------------------------------------------------------------------------
async def _sync_yahoo(settings_dict):
    """Drive _sync_yahoo_settings with a canned get_league_settings result."""
    svc = _service()
    lg = _blank_league()
    lg.league_id = "12345"
    lg.season_year = 2026

    repo = MagicMock()
    repo.get_yahoo_tokens = AsyncMock(return_value=("tok", "refresh", None))

    with patch("backend.repositories.credential_repo.CredentialRepository",
               return_value=repo), \
         patch("backend.integrations.yahoo_api.get_league_settings",
               new_callable=AsyncMock, return_value=settings_dict):
        await svc._sync_yahoo_settings(lg)
    return lg


def _yahoo_settings(**over):
    base = {
        "name": "Y League", "num_teams": 12, "draft_type": "snake",
        "scoring_type": "ppr", "auction_budget": None, "draft_date": None,
        "trade_deadline": "2026-11-15", "waiver_type": None,
        "playoff_start_week": 15, "uses_faab": None, "roster_slots": None,
    }
    base.update(over)
    return base


async def test_yahoo_faab_league_is_recorded_as_bidding_with_no_budget():
    """Yahoo says the league bids. No Yahoo field for the league's FAAB budget has
    been confirmed against a live response, so the budget stays NULL rather than
    being guessed — the waiver page then states $100 as an assumption."""
    lg = await _sync_yahoo(_yahoo_settings(uses_faab=True, waiver_type="faab"))

    assert lg.uses_bidding_budget is True
    assert lg.waiver_budget is None
    assert lg.waiver_type == "faab"


async def test_yahoo_explicit_no_faab_is_recorded_as_not_bidding():
    lg = await _sync_yahoo(_yahoo_settings(uses_faab=False, waiver_type="continual"))

    assert lg.uses_bidding_budget is False
    assert lg.waiver_budget is None


async def test_yahoo_unknown_leaves_the_answer_unset():
    """Yahoo told us nothing about the waiver system. Nothing is claimed."""
    lg = await _sync_yahoo(_yahoo_settings(uses_faab=None))

    assert lg.uses_bidding_budget is None
    assert lg.waiver_budget is None


async def test_yahoo_overlong_waiver_type_is_capped_to_the_column_width():
    """waiver_type is a VARCHAR(30) and Yahoo's raw string is not a value we
    control. An over-long one must not abort the whole settings sync."""
    lg = await _sync_yahoo(_yahoo_settings(waiver_type="x" * 60))

    assert len(lg.waiver_type) == 30
