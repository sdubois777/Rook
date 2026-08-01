"""Tests for Sleeper LeaguePlatformAPI implementation."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.integrations.sleeper_league_api import SleeperLeagueAPI


def _make_league(league_id="12345"):
    league = MagicMock()
    league.league_id = league_id
    league.platform = "sleeper"
    return league


@pytest.mark.asyncio
async def test_get_rosters_maps_user_display_name():
    league = _make_league()
    api = SleeperLeagueAPI(league)

    rosters_data = [
        {
            "roster_id": 1,
            "owner_id": "user_abc",
            "players": ["4040715", "3116406"],
            "settings": {"wins": 7, "losses": 3, "waiver_budget_used": 50},
        }
    ]
    users_data = [
        {
            "user_id": "user_abc",
            "display_name": "AlphaManager",
            "metadata": {"team_name": "Alpha Squad"},
        }
    ]

    call_count = 0

    async def mock_get(path):
        nonlocal call_count
        call_count += 1
        if "rosters" in path:
            return rosters_data
        if "users" in path:
            return users_data
        return []

    with patch.object(api, "_get", side_effect=mock_get):
        rosters = await api.get_rosters()
        assert len(rosters) == 1
        assert rosters[0].manager_name == "AlphaManager"
        assert rosters[0].team_name == "Alpha Squad"
        assert len(rosters[0].players) == 2
        assert rosters[0].wins == 7
        assert rosters[0].losses == 3


@pytest.mark.asyncio
async def test_get_draft_picks_with_auction_price():
    league = _make_league()
    api = SleeperLeagueAPI(league)

    drafts_data = [{"draft_id": "draft_001"}]
    picks_data = [
        {
            "player_id": "4040715",
            "roster_id": 1,
            "pick_no": 1,
            "round": 1,
            "amount": 55,
            "metadata": {
                "first_name": "Josh",
                "last_name": "Allen",
                "position": "QB",
                "team": "BUF",
            },
        },
        {
            "player_id": "3116406",
            "roster_id": 2,
            "pick_no": 2,
            "round": 1,
            "amount": None,
            "metadata": {
                "first_name": "Christian",
                "last_name": "McCaffrey",
                "position": "RB",
                "team": "SF",
            },
        },
    ]

    async def mock_get(path):
        if "drafts" in path:
            return drafts_data
        if "picks" in path:
            return picks_data
        return []

    with patch.object(api, "_get", side_effect=mock_get):
        picks = await api.get_draft_picks()
        assert len(picks) == 2
        assert picks[0].player_name == "Josh Allen"
        assert picks[0].auction_price == 55
        assert picks[0].position == "QB"
        assert picks[1].player_name == "Christian McCaffrey"
        assert picks[1].auction_price is None


@pytest.mark.asyncio
async def test_get_free_agents_returns_empty():
    league = _make_league()
    api = SleeperLeagueAPI(league)
    result = await api.get_free_agents()
    assert result == []


# ---------------------------------------------------------------------------
# The league's REAL weekly schedule.
#
# Sleeper returns one entry PER ROSTER, not per game: two rosters sharing a
# matchup_id are playing each other. Until this was implemented the method returned
# an empty list, and the matchup page could not tell "no games" from "not
# implemented" — so it invented a round-robin opponent for every league, every week.
# ---------------------------------------------------------------------------
def _matchup_rows(*pairs, orphan=None, no_game=0):
    """Build a Sleeper matchups payload from (roster_id, roster_id, mid) triples."""
    rows = []
    for a, b, mid in pairs:
        rows.append({"roster_id": a, "matchup_id": mid, "points": 100.0})
        rows.append({"roster_id": b, "matchup_id": mid, "points": 90.0})
    if orphan is not None:
        rows.append({"roster_id": orphan, "matchup_id": 99, "points": 0.0})
    for i in range(no_game):
        rows.append({"roster_id": 900 + i, "matchup_id": None, "points": 0.0})
    return rows


async def test_get_matchups_pairs_rosters_sharing_a_matchup_id():
    api = SleeperLeagueAPI(_make_league())
    rows = _matchup_rows((1, 6, 5), (2, 10, 2), (3, 7, 3))

    async def _get(path):
        assert path.endswith("/matchups/3")
        return rows

    with patch.object(api, "_get", side_effect=_get):
        out = await api.get_matchups(week=3)

    assert len(out) == 3
    assert {(m.home_team_id, m.away_team_id) for m in out} == {("1", "6"), ("10", "2"), ("3", "7")}
    assert all(m.week == 3 for m in out)


async def test_get_matchups_drops_rosters_with_no_game():
    """A null matchup_id means that roster has no game this week (eliminated from
    the playoff bracket). It must not be paired with anything."""
    api = SleeperLeagueAPI(_make_league())
    rows = _matchup_rows((1, 2, 1), no_game=2)

    async def _get(path):
        return rows

    with patch.object(api, "_get", side_effect=_get):
        out = await api.get_matchups(week=15)

    assert len(out) == 1
    ids = {t for m in out for t in (m.home_team_id, m.away_team_id)}
    assert ids == {"1", "2"}
    assert not any(i.startswith("90") for i in ids)


async def test_get_matchups_never_infers_an_opponent_from_a_lone_roster():
    """One roster carrying a matchup_id with nobody else on it is a data oddity, not
    a game. Guessing who it plays is exactly the invention being removed."""
    api = SleeperLeagueAPI(_make_league())
    rows = _matchup_rows((1, 2, 1), orphan=7)

    async def _get(path):
        return rows

    with patch.object(api, "_get", side_effect=_get):
        out = await api.get_matchups(week=4)

    assert len(out) == 1
    assert "7" not in {t for m in out for t in (m.home_team_id, m.away_team_id)}


async def test_get_matchups_returns_none_when_the_fetch_fails():
    """None means "we could not tell", which makes the caller withhold an opponent.
    An empty list would claim the league genuinely has no game this week."""
    api = SleeperLeagueAPI(_make_league())

    async def _boom(path):
        raise RuntimeError("sleeper down")

    with patch.object(api, "_get", side_effect=_boom):
        out = await api.get_matchups(week=1)

    assert out is None


async def test_get_matchups_empty_week_is_a_real_answer_not_unknown():
    """Past the end of the season Sleeper returns an empty list. That is a real
    answer and must stay distinguishable from a failure."""
    api = SleeperLeagueAPI(_make_league())

    async def _get(path):
        return []

    with patch.object(api, "_get", side_effect=_get):
        out = await api.get_matchups(week=99)

    assert out == []
    assert out is not None


@pytest.mark.asyncio
async def test_get_standings_delegates_to_get_rosters():
    league = _make_league()
    api = SleeperLeagueAPI(league)

    with patch.object(
        api, "get_rosters", new_callable=AsyncMock
    ) as mock:
        mock.return_value = ["fake_roster"]
        result = await api.get_standings()
        assert result == ["fake_roster"]
        mock.assert_called_once()


@pytest.mark.asyncio
async def test_roster_missing_owner():
    """Roster with no matching user should still work."""
    league = _make_league()
    api = SleeperLeagueAPI(league)

    rosters_data = [
        {
            "roster_id": 1,
            "owner_id": "unknown_user",
            "players": [],
            "settings": {},
        }
    ]
    users_data = []  # No users returned

    async def mock_get(path):
        if "rosters" in path:
            return rosters_data
        if "users" in path:
            return users_data
        return []

    with patch.object(api, "_get", side_effect=mock_get):
        rosters = await api.get_rosters()
        assert len(rosters) == 1
        assert rosters[0].manager_name == ""


@pytest.mark.asyncio
async def test_get_rosters_captures_owner_and_co_owners():
    """owner_id + co_owners → owner_ids for exact is_me binding (co-owned team)."""
    league = _make_league()
    api = SleeperLeagueAPI(league)
    rosters_data = [
        {"roster_id": 8, "owner_id": "me-777", "co_owners": ["partner-9"], "players": []},
    ]
    users_data = [{"user_id": "me-777", "display_name": "Me", "metadata": {}}]

    async def mock_get(path):
        return rosters_data if "rosters" in path else users_data

    with patch.object(api, "_get", new=mock_get):
        rosters = await api.get_rosters()
    assert rosters[0].platform_team_id == "8"
    assert rosters[0].owner_ids == ["me-777", "partner-9"]   # owner + co-owner


# ---------------------------------------------------------------------------
# Where the MANAGER has each player seated.
#
# Sleeper's `starters` array is slot-ordered: index i is the slot at index i of the
# league's roster_positions once bench entries are dropped. Verified live across a
# large sample. Until this was wired, every rostered player on every real league had
# no slot at all, so the whole roster rendered as bench and the injury-aware lineup
# filters had nothing to work with.
# ---------------------------------------------------------------------------
_SLOTS = ["QB", "RB", "RB", "WR", "WR", "FLEX", "K", "DEF", "BN", "BN", "BN"]


def _slot_league(rosters, positions=None):
    """Fake the three calls get_rosters makes: rosters, users, and the league object
    that carries the ordered slot list."""
    async def _get(path):
        if path.endswith("/rosters"):
            return rosters
        if path.endswith("/users"):
            return [{"user_id": "u1", "display_name": "Me", "metadata": {}}]
        return {"roster_positions": positions if positions is not None else _SLOTS}
    return _get


@pytest.mark.asyncio
async def test_lineup_slots_come_from_the_starters_order():
    """Eight starting slots, eight starters, in order."""
    starters = ["qb1", "rb1", "rb2", "wr1", "wr2", "flexguy", "k1", "def1"]
    rosters = [{"roster_id": 1, "owner_id": "u1",
                "players": starters + ["bench1", "bench2"], "starters": starters}]

    api = SleeperLeagueAPI(_make_league())
    with patch.object(api, "_get", side_effect=_slot_league(rosters)):
        out = await api.get_rosters()

    by_id = {p.platform_player_id: p.lineup_slot for p in out[0].players}
    assert by_id == {
        "qb1": "QB", "rb1": "RB", "rb2": "RB", "wr1": "WR", "wr2": "WR",
        "flexguy": "FLEX", "k1": "K", "def1": "DEF",
        "bench1": "BENCH", "bench2": "BENCH",
    }


@pytest.mark.asyncio
async def test_injured_reserve_is_its_own_slot_not_bench():
    """A player the manager put on injured reserve is not benched by choice — it is
    the strongest availability signal there is, and no league-wide injury feed can
    express it."""
    starters = ["qb1", "rb1", "rb2", "wr1", "wr2", "flexguy", "k1", "def1"]
    rosters = [{"roster_id": 1, "owner_id": "u1",
                "players": starters + ["hurt1", "bench1"],
                "starters": starters, "reserve": ["hurt1"]}]

    api = SleeperLeagueAPI(_make_league())
    with patch.object(api, "_get", side_effect=_slot_league(rosters)):
        out = await api.get_rosters()

    by_id = {p.platform_player_id: p.lineup_slot for p in out[0].players}
    assert by_id["hurt1"] == "IR"
    assert by_id["bench1"] == "BENCH"


@pytest.mark.asyncio
async def test_empty_starter_slot_does_not_shift_every_later_slot():
    """Sleeper writes "0" for an unfilled starting slot. Skipping it while KEEPING
    its index is what stops every slot after it being mislabelled."""
    starters = ["qb1", "0", "rb2", "wr1", "wr2", "flexguy", "k1", "def1"]
    rosters = [{"roster_id": 1, "owner_id": "u1",
                "players": [p for p in starters if p != "0"], "starters": starters}]

    api = SleeperLeagueAPI(_make_league())
    with patch.object(api, "_get", side_effect=_slot_league(rosters)):
        out = await api.get_rosters()

    by_id = {p.platform_player_id: p.lineup_slot for p in out[0].players}
    assert by_id["rb2"] == "RB"        # NOT shifted up into the empty RB slot's place
    assert by_id["def1"] == "DEF"      # the last slot is still right
    assert "0" not in by_id


@pytest.mark.asyncio
async def test_a_shape_we_do_not_recognise_leaves_slots_unknown_not_wrong():
    """More starters than starting slots means we do not understand this league's
    shape. Every slot stays unknown rather than being confidently mislabelled."""
    starters = ["a", "b", "c", "d", "e", "f", "g", "h", "i"]      # 9 vs 8 slots
    rosters = [{"roster_id": 1, "owner_id": "u1", "players": starters, "starters": starters}]

    api = SleeperLeagueAPI(_make_league())
    with patch.object(api, "_get", side_effect=_slot_league(rosters)):
        out = await api.get_rosters()

    assert all(p.lineup_slot is None for p in out[0].players)


@pytest.mark.asyncio
async def test_unreadable_slot_list_never_calls_anyone_benched():
    """The league object carries no slot list at all, so no slot can be named.

    The trap this guards is the tempting default: treat "not in the starters list"
    as benched. When the slot list itself could not be read we do not know who is
    starting, so calling everyone benched is a positive claim about every player on
    every team. Unknown has to stay unknown."""
    starters = ["qb1", "rb1"]
    rosters = [{"roster_id": 1, "owner_id": "u1",
                "players": starters + ["z"], "starters": starters}]

    async def _get(path):
        if path.endswith("/rosters"):
            return rosters
        if path.endswith("/users"):
            return [{"user_id": "u1", "display_name": "Me", "metadata": {}}]
        return {}                      # league object with no roster_positions

    api = SleeperLeagueAPI(_make_league())
    with patch.object(api, "_get", side_effect=_get):
        out = await api.get_rosters()

    slots = [p.lineup_slot for p in out[0].players]
    assert slots == [None, None, None]
    assert "BENCH" not in slots


@pytest.mark.asyncio
async def test_injured_reserve_survives_an_unreadable_slot_list():
    """The reserve list is its own array and does not depend on the slot order, so
    an injured-reserve placement is still reported when nothing else can be."""
    rosters = [{"roster_id": 1, "owner_id": "u1",
                "players": ["a", "hurt1"], "starters": ["a"], "reserve": ["hurt1"]}]

    async def _get(path):
        if path.endswith("/rosters"):
            return rosters
        if path.endswith("/users"):
            return [{"user_id": "u1", "display_name": "Me", "metadata": {}}]
        return {}

    api = SleeperLeagueAPI(_make_league())
    with patch.object(api, "_get", side_effect=_get):
        out = await api.get_rosters()

    by_id = {p.platform_player_id: p.lineup_slot for p in out[0].players}
    assert by_id == {"a": None, "hurt1": "IR"}
