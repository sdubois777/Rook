"""Team-defense picks must resolve deterministically by TEAM, not by name (#461).

The bug: `_resolve_player` took only a name and a Sleeper id, so the position and
team every reader already sends were discarded. A defense therefore fell through
to the name-similarity path, where ESPN's "Lions D/ST" normalises to the words
["lions", "d/st"], the query searches on the LAST word, and `name ILIKE '%d/st%'`
matches no row — our defenses are stored as "Detroit Lions" with position "DEF".
Production logs on 2026-08-24 show exactly that, twice:

    resolve: no eligible candidate for name='Lions D/ST' team=None pos=None
    resolve: no eligible candidate for name='Steelers D/ST' team=None pos=None

Measured impact: 14 of the 192 picks in a complete 12-team ESPN snake draft are
defenses (extension/test/fixtures/espn/snake/complete.html). Every one of them
failed to resolve, so it was never removed from the available list and stayed
eligible to be recommended. ESPN and Yahoo were affected; Sleeper was not,
because it also sends its own player id, which matches exactly first.

These tests pin the routing decision at the ROUTER boundary. The repository's own
DST lookup is covered in tests/unit/utils/test_player_resolver.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.routers.draft as draft


# --- fakes -----------------------------------------------------------------

def _repo_returning(*, dst=None, by_sleeper=None, by_name=None):
    """A PlayerRepository stand-in recording which lookup each test provoked."""
    repo = MagicMock()
    repo.find_by_dst_team = AsyncMock(return_value=dst)
    repo.find_by_sleeper_id = AsyncMock(return_value=by_sleeper)
    repo.find_by_name_fuzzy = AsyncMock(return_value=by_name)
    return repo


def _patch_repo(repo):
    """Patch both the session factory and the repository class.

    `_resolve_player` imports PlayerRepository INSIDE the function, so it must be
    patched at its source module, not on backend.routers.draft.
    """
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock())
    ctx.__aexit__ = AsyncMock(return_value=False)
    return (
        patch("backend.routers.draft.AsyncSessionLocal", return_value=ctx),
        patch("backend.repositories.player_repo.PlayerRepository", return_value=repo),
    )


LIONS = SimpleNamespace(
    id="uuid-det-def", name="Detroit Lions", position="DEF", team_abbr="DET",
    yahoo_player_id="",
)


# --- _resolve_player routing ------------------------------------------------

async def test_espn_defense_resolves_by_team_abbreviation():
    """The exact production failure: ESPN's "Lions D/ST" + "DET" now resolves."""
    repo = _repo_returning(dst=LIONS)
    p_sess, p_repo = _patch_repo(repo)
    with p_sess, p_repo:
        got = await draft._resolve_player(
            "Lions D/ST", None, position="D/ST", team="DET"
        )

    assert got is LIONS
    repo.find_by_dst_team.assert_awaited_once_with("DET")
    # The name path must never be reached — it is what produced the wrong answer.
    repo.find_by_name_fuzzy.assert_not_awaited()


async def test_defense_without_a_team_falls_back_to_the_full_name():
    """find_by_dst_team accepts the stored full name too, so a reader that omits
    the team abbreviation still resolves."""
    repo = _repo_returning(dst=LIONS)
    p_sess, p_repo = _patch_repo(repo)
    with p_sess, p_repo:
        got = await draft._resolve_player(
            "Detroit Lions", None, position="DEF", team=None
        )

    assert got is LIONS
    repo.find_by_dst_team.assert_awaited_once_with("Detroit Lions")


async def test_defense_miss_falls_through_instead_of_returning_none():
    """A team lookup that misses must not be worse than the old behaviour: the
    sleeper-id and name paths still run."""
    fallback = SimpleNamespace(id="uuid-x", name="Detroit Lions", position="DEF")
    repo = _repo_returning(dst=None, by_name=fallback)
    p_sess, p_repo = _patch_repo(repo)
    with p_sess, p_repo:
        got = await draft._resolve_player(
            "Detroit Lions", None, position="D/ST", team="NOPE"
        )

    assert got is fallback
    repo.find_by_dst_team.assert_awaited_once()
    repo.find_by_name_fuzzy.assert_awaited_once_with("Detroit Lions")


@pytest.mark.parametrize("position", ["K", "QB", "RB", "WR", "TE", None])
async def test_non_defense_picks_never_touch_the_team_lookup(position):
    """Kickers are people with ordinary names — routing them by team would break
    a path that works today."""
    kicker = SimpleNamespace(id="uuid-k", name="Cam Little", position="K")
    repo = _repo_returning(by_name=kicker)
    p_sess, p_repo = _patch_repo(repo)
    with p_sess, p_repo:
        got = await draft._resolve_player(
            "Cam Little", None, position=position, team="JAX"
        )

    assert got is kicker
    repo.find_by_dst_team.assert_not_awaited()


async def test_sleeper_id_still_wins_for_a_person():
    """Unchanged: an exact id match short-circuits before any name work."""
    player = SimpleNamespace(id="uuid-s", name="Bijan Robinson", position="RB")
    repo = _repo_returning(by_sleeper=player)
    p_sess, p_repo = _patch_repo(repo)
    with p_sess, p_repo:
        got = await draft._resolve_player("B. ROBINSON", "4035687", position="RB")

    assert got is player
    repo.find_by_name_fuzzy.assert_not_awaited()


# --- _event_team: the readers disagree on the key ---------------------------

def test_event_team_reads_both_reader_spellings():
    """ESPN snake / Yahoo / Sleeper send `nfl_team`; the ESPN auction reader
    sends `pro_team`. Reading only one would cover half the platforms."""
    assert draft._event_team({"nfl_team": "DET"}) == "DET"
    assert draft._event_team({"pro_team": "PIT"}) == "PIT"
    assert draft._event_team({"nfl_team": "DET", "pro_team": "PIT"}) == "DET"
    assert draft._event_team({}) is None
    assert draft._event_team({"nfl_team": None, "pro_team": None}) is None
    assert draft._event_team(None) is None


# --- call sites forward what the readers send -------------------------------

async def test_snake_pick_forwards_position_and_nfl_team(monkeypatch):
    """A real ESPN snake defense payload, shaped as the reader emits it."""
    resolve = AsyncMock(return_value=LIONS)
    monkeypatch.setattr(draft, "_resolve_player", resolve)
    payload = {
        "pick_number": 140,
        "player_name": "Lions D/ST",
        "position": "D/ST",
        "nfl_team": "DET",
        "espn_player_id": None,
        "picker": "Team 3",
        "is_yours": False,
        "round": 12,
    }
    await draft._record_snake_pick(
        SimpleNamespace(type="snake_pick", platform="espn", payload=payload),
        engine=AsyncMock(),
        state=MagicMock(),
    )

    resolve.assert_awaited_once_with(
        "Lions D/ST", None, position="D/ST", team="DET"
    )
    # Enriched, so the draft room can match and remove it by id.
    assert payload["id"] == "uuid-det-def"
    assert payload["player_name"] == "Detroit Lions"
    assert payload["position"] == "DEF"


async def test_auction_pick_forwards_position_and_pro_team(monkeypatch):
    """The ESPN auction reader names the team field `pro_team`."""
    resolve = AsyncMock(return_value=LIONS)
    monkeypatch.setattr(draft, "_resolve_player", resolve)
    engine = AsyncMock()
    payload = {
        "player_name": "Lions D/ST",
        "position": "D/ST",
        "pro_team": "DET",
        "final_price": 2,
        "winner": "Team 3",
        "is_yours": False,
    }
    await draft._record_pick(
        SimpleNamespace(type="draft_pick", platform="espn", payload=payload),
        engine,
        MagicMock(),
    )

    resolve.assert_awaited_once_with(
        "Lions D/ST", None, position="D/ST", team="DET"
    )
    assert payload["player_name"] == "Detroit Lions"


async def test_nomination_forwards_position_and_pro_team(monkeypatch):
    """A nominated defense must reach the room with its canonical name, or the
    nominee card shows the raw board text and the rec cannot match it."""
    resolve = AsyncMock(return_value=LIONS)
    monkeypatch.setattr(draft, "_resolve_player", resolve)
    payload = {
        "player_name": "Lions D/ST",
        "position": "D/ST",
        "pro_team": "DET",
        "opening_bid": 1,
    }
    await draft._enrich_nomination(
        SimpleNamespace(type="nomination", platform="espn", payload=payload)
    )

    resolve.assert_awaited_once_with(
        "Lions D/ST", None, position="D/ST", team="DET"
    )
    assert payload["player_name"] == "Detroit Lions"
    assert payload["player_id"] == "uuid-det-def"
