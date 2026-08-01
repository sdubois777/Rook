"""Reading each platform's REAL waiver settings — and refusing to invent one.

The defect these guard: the waiver page told every customer they had "$100 of $100
budget left" from a budget that was never read from their league, and labelled
rolling-waiver leagues (which never bid) as budget leagues.

The trap that shapes every test here is that EVERY platform reports a budget value
even for leagues that never bid — measured on 285 of 285 live Sleeper leagues and
34 of 35 live non-bidding ESPN leagues, almost always as exactly 100. So each
platform test feeds a non-bidding league that DOES carry a budget of 100 and
asserts the budget is not reported. A test that omitted the budget from the fixture
would pass against the very bug this exists to prevent.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.espn_league_api import ESPNLeagueAPI
from backend.integrations.sleeper_league_api import SleeperLeagueAPI
from backend.integrations.yahoo_api import _parse_yahoo_uses_faab


# ---------------------------------------------------------------------------
# Sleeper — settings.waiver_type is an integer; 2 bids, 0 and 1 do not.
# ---------------------------------------------------------------------------
def _sleeper_api(league_id="12345"):
    league = MagicMock()
    league.league_id = league_id
    league.platform = "sleeper"
    return SleeperLeagueAPI(league)


def _sleeper_get(league_payload):
    """Fake _get: the league object for /league/{id}, nothing for /drafts."""
    async def _get(path):
        if path.endswith("/drafts"):
            return []
        return league_payload
    return _get


@pytest.mark.parametrize("waiver_type,expected_label", [
    (0, "rolling priority"),
    (1, "reverse standings"),
])
async def test_sleeper_non_bidding_league_reports_no_budget(waiver_type, expected_label):
    """waiver_type 0/1 → the league does not bid, and the vestigial waiver_budget
    of 100 that Sleeper ships anyway is NOT reported as the league's budget."""
    api = _sleeper_api()
    payload = {
        "name": "Rolling Waivers League",
        "total_rosters": 12,
        # 100 is present on purpose — this is the exact value that used to leak
        # through as a fabricated budget.
        "settings": {"waiver_type": waiver_type, "waiver_budget": 100},
    }
    with patch.object(api, "_get", side_effect=_sleeper_get(payload)):
        meta = await api.get_league_metadata()

    assert meta.uses_bidding_budget is False
    assert meta.waiver_budget is None
    assert meta.waiver_system == expected_label


async def test_sleeper_bidding_league_reports_its_real_budget():
    """waiver_type 2 → the league bids, and the budget is the league's own value,
    not the 100 the old code hardcoded."""
    api = _sleeper_api()
    payload = {
        "name": "FAAB League",
        "total_rosters": 10,
        "settings": {"waiver_type": 2, "waiver_budget": 250},
    }
    with patch.object(api, "_get", side_effect=_sleeper_get(payload)):
        meta = await api.get_league_metadata()

    assert meta.uses_bidding_budget is True
    assert meta.waiver_budget == 250
    assert meta.waiver_system == "budget"


async def test_sleeper_absent_waiver_type_stays_unknown():
    """No waiver_type at all → unknown. NOT False, which would be a claim that the
    league does not bid; and no budget, despite one being present."""
    api = _sleeper_api()
    payload = {"name": "L", "total_rosters": 12, "settings": {"waiver_budget": 100}}
    with patch.object(api, "_get", side_effect=_sleeper_get(payload)):
        meta = await api.get_league_metadata()

    assert meta.uses_bidding_budget is None
    assert meta.waiver_budget is None
    assert meta.waiver_system is None


async def test_sleeper_roster_carries_spend_and_position_not_remaining():
    """waiver_budget_used is the amount SPENT. It used to be assigned to a field
    named faab_remaining, so a team that had spent everything looked untouched."""
    api = _sleeper_api()
    rosters = [{
        "roster_id": 1, "owner_id": "u1", "players": [],
        "settings": {"waiver_budget_used": 73, "waiver_position": 4,
                     "wins": 2, "losses": 1},
    }]
    users = [{"user_id": "u1", "display_name": "Manager", "metadata": {}}]

    async def _get(path):
        return rosters if "rosters" in path else users

    with patch.object(api, "_get", side_effect=_get):
        out = await api.get_rosters()

    assert out[0].budget_spent == 73
    assert out[0].waiver_position == 4


# ---------------------------------------------------------------------------
# ESPN — isUsingAcquisitionBudget is the only field that decides bidding.
# ---------------------------------------------------------------------------
def _espn_api():
    league = MagicMock()
    league.league_id = "999"
    league.season_year = 2026
    league.platform = "espn"
    return ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")


async def test_espn_non_bidding_league_reports_no_budget():
    """isUsingAcquisitionBudget false → no budget reported, even though the league
    still carries acquisitionBudget 100 (34 of 35 live non-bidding leagues did)."""
    api = _espn_api()
    payload = {"settings": {
        "name": "Waiver Priority League", "size": 12,
        "acquisitionSettings": {
            "isUsingAcquisitionBudget": False,
            "acquisitionBudget": 100,
            "acquisitionType": "WAIVERS_TRADITIONAL",
        },
    }}
    with patch.object(api, "_get", new_callable=AsyncMock, return_value=payload):
        meta = await api.get_league_metadata()

    assert meta.uses_bidding_budget is False
    assert meta.waiver_budget is None


async def test_espn_acquisition_type_alone_never_means_bidding():
    """WAIVERS_TRADITIONAL appears on live leagues with the budget flag BOTH true
    (17) and false (34). It says when claims process, not whether they are bid on,
    so it must never be read as evidence of bidding."""
    api = _espn_api()
    payload = {"settings": {
        "name": "L", "size": 12,
        "acquisitionSettings": {
            "acquisitionBudget": 100,
            "acquisitionType": "WAIVERS_TRADITIONAL",
            # isUsingAcquisitionBudget deliberately absent
        },
    }}
    with patch.object(api, "_get", new_callable=AsyncMock, return_value=payload):
        meta = await api.get_league_metadata()

    assert meta.uses_bidding_budget is None
    assert meta.waiver_budget is None


async def test_espn_free_agency_league_is_labelled_with_the_shared_constant():
    """A league with no waiver process must carry the exact label that downstream
    code checks to withhold both a budget and a waiver order position. Comparing
    against the shared constant rather than a copy of the string keeps the two ends
    from drifting apart silently."""
    from backend.integrations.platform_models import NO_WAIVERS_SYSTEM

    api = _espn_api()
    payload = {"settings": {
        "name": "No Waivers", "size": 10,
        "acquisitionSettings": {
            "isUsingAcquisitionBudget": False,
            "acquisitionBudget": 100,
            "acquisitionType": "FREEAGENCY",
        },
    }}
    with patch.object(api, "_get", new_callable=AsyncMock, return_value=payload):
        meta = await api.get_league_metadata()

    assert meta.waiver_system == NO_WAIVERS_SYSTEM
    assert meta.uses_bidding_budget is False
    assert meta.waiver_budget is None


async def test_espn_bidding_league_reports_its_real_budget():
    api = _espn_api()
    payload = {"settings": {
        "name": "FAAB", "size": 12,
        "acquisitionSettings": {
            "isUsingAcquisitionBudget": True,
            "acquisitionBudget": 300,
            "acquisitionType": "WAIVERS_TRADITIONAL",
        },
    }}
    with patch.object(api, "_get", new_callable=AsyncMock, return_value=payload):
        meta = await api.get_league_metadata()

    assert meta.uses_bidding_budget is True
    assert meta.waiver_budget == 300
    assert meta.waiver_system == "budget"


async def test_espn_rosters_carry_spend_and_waiver_rank():
    """acquisitionBudgetSpent and waiverRank are in the mTeam response the roster
    fetch already makes; both were previously discarded."""
    api = _espn_api()
    roster_payload = {"teams": [{"id": 1, "name": "Team A", "roster": {"entries": []}}]}
    team_payload = {"teams": [{
        "id": 1, "name": "Team A", "owners": ["{OWNER}"],
        "transactionCounter": {"acquisitionBudgetSpent": 42},
        "waiverRank": 7,
    }]}

    async def _get(view):
        return roster_payload if view == "mRoster" else team_payload

    with patch.object(api, "_get", side_effect=_get):
        rosters = await api.get_rosters()

    assert rosters[0].budget_spent == 42
    assert rosters[0].waiver_position == 7


# ---------------------------------------------------------------------------
# Yahoo — UNVERIFIED field names. Absence must read as unknown, never as False.
# ---------------------------------------------------------------------------
def test_yahoo_absent_waiver_fields_are_unknown_not_false():
    """The whole point. The previous code returned False here, which is a positive
    claim that the league does not bid — it would have told a Yahoo FAAB customer
    their league claims by priority and withheld the bid entirely."""
    assert _parse_yahoo_uses_faab({}) is None
    assert _parse_yahoo_uses_faab({"waiver_type": ""}) is None


def test_yahoo_own_flag_is_authoritative_both_ways():
    """Yahoo answering the question directly is a real answer in both directions."""
    assert _parse_yahoo_uses_faab({"uses_faab": "1"}) is True
    assert _parse_yahoo_uses_faab({"uses_faab": "0"}) is False
    assert _parse_yahoo_uses_faab({"uses_faab": 1}) is True
    assert _parse_yahoo_uses_faab({"uses_faab": 0}) is False


def test_yahoo_flag_beats_a_contradicting_waiver_type_string():
    """The documented flag wins; the string is only a fallback."""
    assert _parse_yahoo_uses_faab({"uses_faab": "0", "waiver_type": "faab"}) is False


def test_yahoo_waiver_type_string_can_only_raise_to_true():
    """An unrecognised rule string stays unknown. Reading it as "does not bid" is
    the guess this must not make."""
    assert _parse_yahoo_uses_faab({"waiver_type": "faab"}) is True
    assert _parse_yahoo_uses_faab({"waiver_type": "continual"}) is None
    assert _parse_yahoo_uses_faab({"waiver_type": "gametime"}) is None


async def _yahoo_league_settings(settings_block):
    """Call the real get_league_settings against a canned Yahoo response, so the
    seam BETWEEN the parser and the sync is covered and not just its two ends."""
    from unittest.mock import AsyncMock, patch

    from backend.integrations.yahoo_api import get_league_settings

    payload = {"fantasy_content": {"league": [
        {"name": "Y", "num_teams": 12, "season": "2026"},
        {"settings": [{
            "stat_modifiers": {"stats": [{"stat": {"stat_id": "11", "value": "1.00"}}]},
            "playoff_start_week": "15", "is_auction_draft": 0,
            **settings_block,
        }]},
    ]}}
    with patch("backend.integrations.yahoo_api._api_get_with_token",
               new_callable=AsyncMock, return_value=payload):
        return await get_league_settings("tok", "470.l.12345")


async def test_yahoo_unknown_survives_the_trip_out_of_get_league_settings():
    """Unknown must still be unknown by the time the sync sees it. Both ends of this
    seam are tested; without this, a bool() coercion in between would convert every
    unknown into "this league does not bid" and go unnoticed — which would tell a
    real Yahoo bidding customer their league claims by priority and withhold the bid
    entirely."""
    out = await _yahoo_league_settings({})
    assert out["uses_faab"] is None            # not False
    assert out["waiver_type"] is None


async def test_yahoo_known_answers_survive_the_same_trip():
    assert (await _yahoo_league_settings({"uses_faab": "1"}))["uses_faab"] is True
    assert (await _yahoo_league_settings({"uses_faab": "0"}))["uses_faab"] is False
    faab = await _yahoo_league_settings({"waiver_type": "faab"})
    assert faab["uses_faab"] is True and faab["waiver_type"] == "faab"
