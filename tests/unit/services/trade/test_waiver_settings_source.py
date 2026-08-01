"""build_real_waiver_source — turning a league's stored waiver settings into what
the waiver page is allowed to say.

Three behaviours are load-bearing and each has a failure mode that reaches a paying
customer:

  * remaining = budget MINUS spend. The platforms report SPEND. The field that held
    it was previously named faab_remaining, so a team that had spent its whole
    budget was displayed as still holding all of it.
  * a league that does not bid gets NO budget and NO balance, however many budget
    numbers the platform ships alongside.
  * a league whose waiver system could not be read gets nothing at all — not a
    default, not a guess.
"""
from __future__ import annotations

import pandas as pd

from backend.integrations.platform_models import RosteredPlayer, TeamRoster
from backend.services.trade import real_league_source as rls
from backend.services.trade.real_league_source import build_real_waiver_source


class _FakePlayer:
    def __init__(self, pid, name, position, team="NFL"):
        self.id = pid
        self.name = name
        self.position = position
        self.team_abbr = team


class _User:
    id = "u1"


def _league(**waiver):
    """A synced league record. `waiver` sets uses_bidding_budget / waiver_budget /
    waiver_type — everything else is the minimum build_real_league_source reads."""
    class _League:
        platform = "sleeper"
        season_year = 2025
        roster_slots = {"QB": 1, "RB": 2, "WR": 2, "K": 1, "DEF": 1, "BN": 5}
        id = "L1"
        scoring = "ppr"
        draft_status = "complete"
        draft_date = None
        my_team_id = "t1"
        uses_bidding_budget = waiver.get("uses_bidding_budget")
        waiver_budget = waiver.get("waiver_budget")
        waiver_type = waiver.get("waiver_type")
    return _League()


def _rosters():
    """Two teams with DIFFERENT spend, so a test cannot pass by coincidence."""
    return [
        TeamRoster(platform_team_id="t1", manager_name="Me", team_name="My Team",
                   players=[RosteredPlayer(platform_player_id="456",
                                           player_name="Malik Nabers",
                                           position="WR", team_abbr="NYG")],
                   budget_spent=37, waiver_position=2),
        TeamRoster(platform_team_id="t2", manager_name="Them", team_name="Their Team",
                   players=[], budget_spent=0, waiver_position=9),
    ]


def _patch(monkeypatch):
    """No DB: fake the player resolver, the FA pool and the prior loader."""
    async def _resolve(self, *, sleeper_id=None, espn_id=None, yahoo_id=None,
                       gsis_id=None, sportradar_id=None, name=None,
                       position=None, team=None):
        if sleeper_id == "456":
            return _FakePlayer("p1", "Malik Nabers", "WR", "NYG")
        return None
    from backend.repositories.player_repo import PlayerRepository
    monkeypatch.setattr(PlayerRepository, "resolve_player", _resolve)
    monkeypatch.setattr(rls, "get_current_nfl_week", lambda season: 7)

    async def _no_pool(db, weekly, rostered):
        return []
    monkeypatch.setattr(rls, "_derive_pool", _no_pool)

    async def _no_priors(db, ids, scoring_format="ppr"):
        return {}
    monkeypatch.setattr("backend.services.trade.trade_demo_source._load_priors", _no_priors)


async def _build(monkeypatch, league):
    _patch(monkeypatch)
    return await build_real_waiver_source(
        None, _User(), user_league=league, team_rosters=_rosters(),
        weekly_usage=pd.DataFrame(), my_team_id="t1",
    )


async def test_remaining_is_budget_minus_spend_not_spend(monkeypatch):
    """$100 budget, $37 spent → $63 left. The number 37 must not appear as a
    balance anywhere: that was the old bug, spend rendered as remaining."""
    src = await _build(monkeypatch, _league(
        uses_bidding_budget=True, waiver_budget=100, waiver_type="budget"))

    assert src.uses_bidding_budget is True
    assert src.faab_budget == 100
    assert src.faab_remaining_by_team["t1"] == 63
    assert src.faab_remaining_by_team["t2"] == 100      # spent nothing → full budget
    assert 37 not in src.faab_remaining_by_team.values()


async def test_remaining_keys_match_the_team_ids_the_router_looks_up(monkeypatch):
    """The balance map is keyed on str(platform_team_id); the router looks it up by
    LeagueState team_id. If those two key spaces ever diverge, every lookup silently
    returns None and every customer's balance reads as unknown — so this performs
    the join exactly the way the router does and checks the values that come back."""
    src = await _build(monkeypatch, _league(
        uses_bidding_budget=True, waiver_budget=100, waiver_type="budget"))

    looked_up = {
        t.team_id: src.faab_remaining_by_team.get(t.team_id) for t in src.state.teams
    }
    assert looked_up == {"t1": 63, "t2": 100}


async def test_non_bidding_league_gets_no_budget_and_no_balance(monkeypatch):
    """The league does not bid. Waiver order is offered instead, and there is no
    dollar figure of any kind — not a budget, not a balance."""
    src = await _build(monkeypatch, _league(
        uses_bidding_budget=False, waiver_budget=100, waiver_type="rolling priority"))

    assert src.uses_bidding_budget is False
    assert src.faab_budget is None
    assert src.faab_remaining_by_team == {}
    assert src.waiver_type == "rolling priority"
    assert src.waiver_position_by_team == {"t1": 2, "t2": 9}


async def test_free_agency_league_gets_no_waiver_order_position(monkeypatch):
    """A league with no waiver process at all has no waiver order, so it must not be
    given one — that is the same fabricated-league-mechanic defect as the $100
    budget, in a field that happens not to be money.

    ESPN reports a per-team waiverRank on every team of every league regardless of
    whether the league uses waivers (present on all 176 sampled live leagues,
    including every free-agency one), exactly like the vestigial budget of 100. So
    the league's waiver system, not the presence of the field, has to authorise it."""
    src = await _build(monkeypatch, _league(
        uses_bidding_budget=False, waiver_budget=100,
        waiver_type="free agency, no waivers"))

    assert src.waiver_position_by_team == {}
    assert src.faab_budget is None


async def test_priority_leagues_still_get_their_waiver_order(monkeypatch):
    """The guard above must not suppress the position for leagues that do run
    waivers — that is the whole point of showing it."""
    src = await _build(monkeypatch, _league(
        uses_bidding_budget=False, waiver_type="continuous waiver priority"))

    assert src.waiver_position_by_team == {"t1": 2, "t2": 9}


async def test_unknown_waiver_system_claims_nothing(monkeypatch):
    """Waiver system unreadable → no budget, no balance, no waiver order. Unknown
    is not a licence to fall back to a default."""
    src = await _build(monkeypatch, _league())

    assert src.uses_bidding_budget is None
    assert src.faab_budget is None
    assert src.faab_remaining_by_team == {}
    assert src.waiver_position_by_team == {}


async def test_bidding_league_with_unreadable_budget_states_no_balance(monkeypatch):
    """The league bids but the amount could not be read. No per-team balance is
    invented; the router is what states the $100 assumption, and it labels it."""
    src = await _build(monkeypatch, _league(
        uses_bidding_budget=True, waiver_budget=None, waiver_type="budget"))

    assert src.uses_bidding_budget is True
    assert src.faab_budget is None
    assert src.faab_remaining_by_team == {}


async def test_unreported_spend_is_not_treated_as_zero_spend(monkeypatch):
    """"We do not know what this team has spent" and "this team has spent nothing"
    are different statements. Both ESPN and Sleeper omit the spend field on a team
    that has never claimed, and ESPN reports nothing for any team when its per-team
    request fails — so treating the missing value as 0 hands that team the full
    budget as a balance nobody read."""
    rosters = [
        TeamRoster(platform_team_id="t1", manager_name="Me", team_name="T1",
                   players=[], budget_spent=None),      # never reported
        TeamRoster(platform_team_id="t2", manager_name="Them", team_name="T2",
                   players=[], budget_spent=25),        # reported
    ]
    _patch(monkeypatch)
    src = await build_real_waiver_source(
        None, _User(),
        user_league=_league(uses_bidding_budget=True, waiver_budget=100,
                            waiver_type="budget"),
        team_rosters=rosters, weekly_usage=pd.DataFrame(), my_team_id="t1",
    )

    assert "t1" not in src.faab_remaining_by_team      # absent, NOT 100
    assert src.faab_remaining_by_team == {"t2": 75}


async def test_spend_over_budget_floors_at_zero_never_negative(monkeypatch):
    """A team that has somehow spent more than the budget reads as $0 left, not as
    a negative balance."""
    rosters = [TeamRoster(platform_team_id="t1", manager_name="Me", team_name="T",
                          players=[], budget_spent=140)]
    _patch(monkeypatch)
    src = await build_real_waiver_source(
        None, _User(),
        user_league=_league(uses_bidding_budget=True, waiver_budget=100,
                            waiver_type="budget"),
        team_rosters=rosters, weekly_usage=pd.DataFrame(), my_team_id="t1",
    )
    assert src.faab_remaining_by_team["t1"] == 0
