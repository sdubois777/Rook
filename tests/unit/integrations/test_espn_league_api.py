"""Tests for ESPN LeaguePlatformAPI implementation."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.espn_league_api import ESPNLeagueAPI, _ESPN_POS


def _make_league(league_id="12345", season_year=2026):
    league = MagicMock()
    league.league_id = league_id
    league.season_year = season_year
    league.platform = "espn"
    league.user_id = "user-1"
    return league



def _espn_api():
    """The API object under test, with a fake league and cookies."""
    return ESPNLeagueAPI(league=_make_league(), espn_s2="s2", swid="{SWID}")


def test_espn_position_mapping():
    assert _ESPN_POS[1] == "QB"
    assert _ESPN_POS[2] == "RB"
    assert _ESPN_POS[3] == "WR"
    assert _ESPN_POS[4] == "TE"
    assert _ESPN_POS[5] == "K"
    assert _ESPN_POS[16] == "DEF"


def test_init_sets_cookies():
    league = _make_league()
    api = ESPNLeagueAPI(
        league=league, espn_s2="test_s2", swid="{TEST-SWID}"
    )
    assert api._cookies == {"espn_s2": "test_s2", "SWID": "{TEST-SWID}"}


@pytest.mark.asyncio
async def test_validate_cookies_calls_msettings():
    league = _make_league()
    api = ESPNLeagueAPI(
        league=league, espn_s2="s2", swid="{SWID}"
    )
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"settings": {}}
        result = await api.validate_cookies()
        assert result is True
        mock_get.assert_called_once_with("mSettings")


@pytest.mark.asyncio
async def test_get_rosters_parses_teams():
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "teams": [
            {
                "id": 1,
                "name": "Team Alpha",
                "roster": {
                    "entries": [
                        {
                            "playerPoolEntry": {
                                "player": {
                                    "id": 4040715,
                                    "fullName": "Josh Allen",
                                    "defaultPositionId": 1,
                                }
                            }
                        }
                    ]
                },
            }
        ]
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        rosters = await api.get_rosters()
        assert len(rosters) == 1
        assert rosters[0].team_name == "Team Alpha"
        assert rosters[0].players[0].player_name == "Josh Allen"
        assert rosters[0].players[0].position == "QB"


@pytest.mark.asyncio
async def test_get_draft_picks_parses_auction():
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "draftDetail": {
            "picks": [
                {
                    "playerId": 4040715,
                    "teamId": 1,
                    "overallPickNumber": 1,
                    "roundId": 1,
                    "bidAmount": 55,
                },
                {
                    "playerId": 3116406,
                    "teamId": 2,
                    "overallPickNumber": 2,
                    "roundId": 1,
                    "bidAmount": 48,
                },
            ]
        }
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        picks = await api.get_draft_picks()
        assert len(picks) == 2
        assert picks[0].auction_price == 55
        assert picks[0].pick_number == 1
        assert picks[1].picked_by_team_id == "2"


@pytest.mark.asyncio
async def test_detect_draft_type_auction():
    """Picks with bidAmount > 0 → auction."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "draftDetail": {
            "picks": [
                {"playerId": 1, "bidAmount": 55},
                {"playerId": 2, "bidAmount": 0},
            ]
        }
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction"
    assert budget == 200


@pytest.mark.asyncio
async def test_detect_draft_type_snake():
    """All picks with bidAmount=0 or None → snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {
        "draftDetail": {
            "picks": [
                {"playerId": 1, "bidAmount": 0},
                {"playerId": 2},  # no bidAmount key
            ]
        }
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_detect_draft_type_no_picks():
    """Empty picks list → default snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    mock_response = {"draftDetail": {"picks": []}}
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake"
    assert budget is None


@pytest.mark.asyncio
async def test_detect_draft_type_api_error_defaults_snake():
    """API failure → graceful fallback to snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = Exception("API timeout")
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake"
    assert budget is None


def _view_dispatch(views):
    """AsyncMock side_effect returning a per-view response (mSettings / mDraftDetail)."""
    async def _g(view, season=None):
        return views.get(view, {})
    return _g


@pytest.mark.asyncio
async def test_detect_auction_from_draftsettings_reads_real_budget():
    """The fix: draftSettings.type='AUCTION' → auction + the REAL auctionBudget, WITHOUT
    any bidAmount (undrafted). The authoritative flag must win over the empty picks that
    used to mis-store undrafted auction leagues as snake."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")

    views = {
        "mSettings": {"settings": {"draftSettings": {"type": "AUCTION", "auctionBudget": 250}}},
        # picks exist pre-draft but all bidAmount 0 — the old path would say snake
        "mDraftDetail": {"draftDetail": {"picks": [{"playerId": 1, "bidAmount": 0}]}},
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction"
    assert budget == 250                              # real budget, NOT hard-coded 200
    mock_get.assert_awaited_once_with("mSettings")     # never consulted mDraftDetail


@pytest.mark.asyncio
async def test_detect_auction_default_budget_when_absent():
    """type='AUCTION' with no auctionBudget → Yahoo-style default 200."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")
    views = {"mSettings": {"settings": {"draftSettings": {"type": "AUCTION"}}}}
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction" and budget == 200


@pytest.mark.asyncio
async def test_detect_snake_from_draftsettings_wins_over_bids():
    """type='SNAKE' → snake + no budget, even if mDraftDetail had bids — the flag is
    authoritative (won't false-auction)."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")
    views = {
        "mSettings": {"settings": {"draftSettings": {"type": "SNAKE"}}},
        "mDraftDetail": {"draftDetail": {"picks": [{"playerId": 1, "bidAmount": 99}]}},
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "snake" and budget is None


@pytest.mark.asyncio
async def test_detect_falls_back_to_bids_when_type_absent():
    """draftSettings.type absent → fall back to the post-draft bidAmount signal."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")
    views = {
        "mSettings": {"settings": {"draftSettings": {}}},   # no type
        "mDraftDetail": {"draftDetail": {"picks": [{"playerId": 1, "bidAmount": 40}]}},
    }
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(views)
        draft_type, budget = await api.detect_draft_type()
    assert draft_type == "auction" and budget == 200      # via fallback


@pytest.mark.asyncio
async def test_create_raises_without_cookies():
    league = _make_league()

    with patch(
        "backend.integrations.espn_league_api.CredentialRepository"
    ) as MockRepo:
        mock_repo_instance = AsyncMock()
        mock_repo_instance.get_espn_cookies.return_value = None
        MockRepo.return_value = mock_repo_instance

        from backend.core.exceptions import AppError
        with pytest.raises(AppError, match="ESPN is not connected"):
            await ESPNLeagueAPI.create(league, AsyncMock())


@pytest.mark.asyncio
async def test_get_rosters_captures_owner_swids_from_mteam():
    """ESPN owner SWIDs come from mTeam.owners[] (all owners), not mRoster — populated
    onto owner_ids for exact is_me binding."""
    league = _make_league()
    api = ESPNLeagueAPI(league=league, espn_s2="s2", swid="{SWID}")
    views = {
        "mRoster": {"teams": [{"id": 1, "roster": {"entries": []}},
                              {"id": 2, "roster": {"entries": []}}]},
        "mTeam": {"teams": [
            {"id": 1, "name": "Alpha", "owners": ["{OWNER-A}"], "primaryOwner": "{OWNER-A}"},
            {"id": 2, "name": "Beta", "owners": ["{ME-SWID}", "{CO-OWNER}"]},
        ]},
    }
    async def _g(view, season=None):
        return views.get(view, {})
    with patch.object(api, "_get", new=_g):
        rosters = await api.get_rosters()
    by_id = {r.platform_team_id: r for r in rosters}
    assert by_id["1"].owner_ids == ["{OWNER-A}"]
    assert by_id["2"].owner_ids == ["{ME-SWID}", "{CO-OWNER}"]   # all owners, not just primary


# ---------------------------------------------------------------------------
# The league's REAL weekly schedule, and the two roster fields that were discarded.
#
# Verified against a real ESPN league: the mMatchup view carries the WHOLE season in
# one `schedule` array, 84 entries for a 12-team league, present before any game is
# played. Until this was implemented the method returned an empty list and the
# matchup page invented a round-robin opponent for every league, every week.
# ---------------------------------------------------------------------------
def _game(mid, home, away, *, winner="UNDECIDED", hp=0.0, ap=0.0, one_sided=False):
    entry = {"id": mid * 100 + home, "matchupPeriodId": mid, "winner": winner,
             "home": {"teamId": home, "totalPoints": hp}}
    if not one_sided:
        entry["away"] = {"teamId": away, "totalPoints": ap}
    return entry


@pytest.mark.asyncio
async def test_get_matchups_returns_only_the_requested_week():
    """The schedule array holds the whole season, so filtering on the fantasy week
    is what makes the opponent right. Reading the wrong field here would put every
    league against the wrong team."""
    api = _espn_api()
    schedule = [_game(1, 5, 3), _game(1, 4, 7), _game(2, 5, 7), _game(3, 1, 2)]
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"schedule": schedule}
        out = await api.get_matchups(1)

    assert len(out) == 2
    assert {(m.home_team_id, m.away_team_id) for m in out} == {("5", "3"), ("4", "7")}
    assert all(m.week == 1 for m in out)


@pytest.mark.asyncio
async def test_get_matchups_marks_played_games_complete():
    api = _espn_api()
    schedule = [_game(1, 5, 3, winner="HOME", hp=120.5, ap=99.0),
                _game(2, 5, 3)]
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"schedule": schedule}
        played = (await api.get_matchups(1))[0]
        upcoming = (await api.get_matchups(2))[0]

    assert played.is_complete is True and played.home_score == 120.5
    assert upcoming.is_complete is False


@pytest.mark.asyncio
async def test_get_matchups_skips_a_one_sided_entry():
    """A bye, or a playoff slot with only one side filled. Pairing a team with a
    placeholder would name an opponent that does not exist."""
    api = _espn_api()
    schedule = [_game(1, 5, 3), _game(1, 9, None, one_sided=True)]
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"schedule": schedule}
        out = await api.get_matchups(1)

    assert len(out) == 1
    assert "9" not in {t for m in out for t in (m.home_team_id, m.away_team_id)}


@pytest.mark.asyncio
async def test_get_matchups_returns_none_when_the_fetch_fails():
    """None makes the caller withhold an opponent. An empty list would claim the
    league genuinely has no game this week."""
    api = _espn_api()
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = Exception("ESPN timeout")
        assert await api.get_matchups(1) is None


@pytest.mark.asyncio
async def test_get_matchups_empty_week_is_a_real_answer():
    api = _espn_api()
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {"schedule": [_game(1, 5, 3)]}
        out = await api.get_matchups(20)      # past the end of the season
    assert out == [] and out is not None


def _roster_views(entries):
    return {
        "mRoster": {"teams": [{"id": 1, "name": "T", "roster": {"entries": entries}}]},
        "mTeam": {"teams": [{"id": 1, "name": "T", "owners": ["{O}"]}]},
    }


def _entry(pid, slot_id, injury=None):
    return {"lineupSlotId": slot_id,
            "playerPoolEntry": {"player": {
                "id": pid, "fullName": f"P{pid}", "defaultPositionId": 2,
                "proTeamId": 1, "injuryStatus": injury}}}


@pytest.mark.asyncio
async def test_get_rosters_reads_the_lineup_slot_the_manager_chose():
    """Both of these sit in the response this call already makes and were discarded,
    so every rostered player arrived with no slot and no injury at all."""
    api = _espn_api()
    entries = [_entry(1, 0), _entry(2, 2), _entry(3, 23), _entry(4, 20), _entry(5, 21)]
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(_roster_views(entries))
        rosters = await api.get_rosters()

    slots = {p.platform_player_id: p.lineup_slot for p in rosters[0].players}
    assert slots == {"1": "QB", "2": "RB", "3": "FLEX", "4": "BENCH", "5": "IR"}


@pytest.mark.asyncio
async def test_get_rosters_carries_espns_own_injury_wording_unchanged():
    """The raw platform spelling is kept here on purpose. Normalizing at the reader
    would hide which spelling arrived, and ESPN's INJURY_RESERVE silently produced
    no badge until it was added to the normalizer."""
    api = _espn_api()
    entries = [_entry(1, 0, "ACTIVE"), _entry(2, 2, "QUESTIONABLE"),
               _entry(3, 4, "OUT"), _entry(4, 20, "INJURY_RESERVE"), _entry(5, 20)]
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(_roster_views(entries))
        rosters = await api.get_rosters()

    got = {p.platform_player_id: p.injury_status for p in rosters[0].players}
    assert got == {"1": "ACTIVE", "2": "QUESTIONABLE", "3": "OUT",
                   "4": "INJURY_RESERVE", "5": None}


@pytest.mark.asyncio
async def test_an_unmapped_slot_id_leaves_the_slot_unknown_not_guessed():
    """Slot ids are numbers and carry no meaning of their own, so an id outside the
    confirmed map produces no slot rather than a valid-looking wrong one."""
    api = _espn_api()
    with patch.object(api, "_get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = _view_dispatch(_roster_views([_entry(1, 999), _entry(2, 0)]))
        rosters = await api.get_rosters()

    slots = {p.platform_player_id: p.lineup_slot for p in rosters[0].players}
    assert slots == {"1": None, "2": "QB"}
