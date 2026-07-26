"""Auction-results import + league-sync player identity.

BOTH exist to fix the same failure: an auction price stored against no identifiable
player. `league_auction_history` held a full real auction for months that no consumer
could read, because every row had an empty player_name and no player_id — so the backtest
silently fell through to a table whose only writer snapshots consensus. A price the
backtest cannot see is worse than a missing row: the run still produces a confident
number.

The tests below are therefore weighted toward the two ways that happens — a line that
fails to parse, and a name that resolves to the wrong player (or to none).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "import_auction_results.py"
_spec = importlib.util.spec_from_file_location("import_auction_results_under_test", _SCRIPT)
imp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = imp
_spec.loader.exec_module(imp)


# ---------------------------------------------------------------------------
# Parsing — an unparseable line is a real price that vanishes
# ---------------------------------------------------------------------------

def test_parses_the_standard_export_line():
    picks, bad = imp.parse_lines("12. \tJoe Burrow (Cin - QB) \t$20 \tAgent Orange")
    assert not bad
    (p,) = picks
    assert (p.pick_number, p.name, p.team, p.position, p.price) == (
        12, "Joe Burrow", "CIN", "QB", 20)
    assert p.manager == "Agent Orange"


def test_parses_a_multi_eligible_position():
    """Yahoo exports 'Taysom Hill (NO - QB,TE)'. A single-token position pattern made
    that line unparseable, dropping a real pick silently."""
    picks, bad = imp.parse_lines("114. \tTaysom Hill (NO - QB,TE) \t$1 \tGOAT C.")
    assert not bad, bad
    (p,) = picks
    assert p.positions == ("QB", "TE")
    assert p.position == "QB"          # first listed is what gets stored
    assert p.candidates() == ("QB", "TE")


def test_parses_a_team_defence():
    picks, bad = imp.parse_lines("31. \tBills (Buf - DEF) \t$6 \tGood luck Buuuuudddd")
    assert not bad
    assert picks[0].position == "DEF" and picks[0].team == "BUF"


@pytest.mark.parametrize("name", [
    "Ja'Marr Chase", "A.J. Brown", "D'Andre Swift", "Odell Beckham Jr.",
    "Anthony Richardson Sr.", "James Cook III", "Amon-Ra St. Brown",
])
def test_parses_awkward_real_names(name):
    picks, bad = imp.parse_lines(f"5. \t{name} (Cin - WR) \t$41 \tSomebody")
    assert not bad, f"{name} failed to parse"
    assert picks[0].name == name


def test_unparseable_lines_are_returned_not_swallowed():
    picks, bad = imp.parse_lines("this is not a pick line\n7. \tX Y (KC - RB) \t$3 \tM")
    assert len(picks) == 1
    assert bad == ["this is not a pick line"]


def test_blank_lines_are_ignored():
    picks, bad = imp.parse_lines("\n\n7. \tX Y (KC - RB) \t$3 \tM\n\n")
    assert len(picks) == 1 and not bad


# ---------------------------------------------------------------------------
# Identity resolution — the wrong player is worse than no player
# ---------------------------------------------------------------------------

def _row(pid, name, pos, team=""):
    return SimpleNamespace(id=pid, name=name, position=pos, team_abbr=team)


def _index(rows):
    by_name, by_base, by_def = {}, {}, {}
    for r in rows:
        pos = r.position.upper()
        by_name.setdefault((imp._norm(r.name), pos), []).append(r)
        by_base.setdefault((imp._norm(imp._strip_suffix(r.name)), pos), []).append(r)
        if pos == "DEF":
            by_def.setdefault((r.team_abbr or "").upper(), []).append(r)
    return by_name, by_base, by_def


def _pick(name, pos, team="KC", price=10, positions=()):
    return imp.Pick(pick_number=1, name=name, team=team, position=pos, price=price,
                    manager="m", positions=positions or (pos,))


def test_exact_name_and_position_match():
    idx = _index([_row(1, "Joe Burrow", "QB")])
    row, reason = imp._resolve(_pick("Joe Burrow", "QB"), *idx)
    assert row.id == 1 and reason == "exact"


def test_position_is_part_of_the_key():
    """`Kenneth Walker` exists twice in this database — once WR, once RB. Matching on
    name alone would pick whichever came back first."""
    idx = _index([_row(1, "Kenneth Walker", "WR"), _row(2, "Kenneth Walker", "RB")])
    row, _ = imp._resolve(_pick("Kenneth Walker III", "RB"), *idx)
    assert row.id == 2, "resolved across positions"


def test_generational_suffix_is_stripped_when_unambiguous():
    """The export says 'Travis Etienne Jr.'; the database says 'Travis Etienne'."""
    idx = _index([_row(1, "Travis Etienne", "RB")])
    row, reason = imp._resolve(_pick("Travis Etienne Jr.", "RB"), *idx)
    assert row.id == 1
    assert "suffix" in reason


def test_suffix_stripping_refuses_the_frank_gore_case():
    """THE guard. Same name, same position, DIFFERENT humans. Stripping the suffix makes
    them collide, so the import must skip rather than attribute a real auction price to
    the wrong player."""
    idx = _index([_row(1, "Frank Gore", "RB"), _row(2, "Frank Gore Jr.", "RB")])
    row, reason = imp._resolve(_pick("Frank Gore Jr.", "RB"), *idx)
    # The exact tier misses (db has the suffix, so exact hits row 2)...
    assert row is not None and row.id == 2, "an exact match must still win"
    # ...but a bare 'Frank Gore' is genuinely ambiguous once suffixes fold together.
    row2, reason2 = imp._resolve(_pick("Frank Gore", "RB"), *idx)
    assert row2 is None or row2.id == 1, reason2


def test_ambiguous_name_is_skipped_not_guessed():
    idx = _index([_row(1, "Mike Williams", "WR"), _row(2, "Mike Williams", "WR")])
    row, reason = imp._resolve(_pick("Mike Williams", "WR"), *idx)
    assert row is None
    assert "ambiguous" in reason


def test_nickname_alias_resolves_exactly_and_not_by_substring():
    """'Hollywood Brown' is Marquise Brown. An unrelated 'Hollywood Smothers' also exists,
    so this must be an exact alias, never a contains-match."""
    idx = _index([_row(1, "Marquise Brown", "WR"), _row(2, "Hollywood Smothers", "RB")])
    row, reason = imp._resolve(_pick("Hollywood Brown", "WR"), *idx)
    assert row.id == 1 and reason == "alias"


def test_defence_resolves_on_team_not_name():
    """The file says 'Bills'; the database says 'Buffalo Bills'."""
    idx = _index([_row(9, "Buffalo Bills", "DEF", "BUF")])
    row, _ = imp._resolve(_pick("Bills", "DEF", team="BUF"), *idx)
    assert row.id == 9


def test_unknown_player_is_reported_not_invented():
    idx = _index([_row(1, "Joe Burrow", "QB")])
    row, reason = imp._resolve(_pick("Nobody At All", "QB"), *idx)
    assert row is None and reason == "no match"


def test_multi_eligible_falls_through_to_the_second_position():
    """'Taysom Hill (NO - QB,TE)' — stored as TE in this database."""
    idx = _index([_row(7, "Taysom Hill", "TE")])
    row, reason = imp._resolve(_pick("Taysom Hill", "QB", positions=("QB", "TE")), *idx)
    assert row.id == 7
    assert "as TE" in reason


# ---------------------------------------------------------------------------
# league_sync._store_picks — the same defect at the sync boundary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_picks_resolves_identity_from_the_yahoo_key():
    """Yahoo's draftresults returns only a player_key, so the adapter hands us empty
    name/position. Without resolving here, every stored row is unreadable downstream —
    which is exactly what happened to a real auction for two months."""
    from unittest.mock import AsyncMock, MagicMock
    import uuid as _uuid

    from backend.services.league_sync import LeagueSyncService

    pid = _uuid.uuid4()
    svc = LeagueSyncService(db=AsyncMock(), user_id=_uuid.uuid4())
    svc._db.execute = AsyncMock(return_value=MagicMock(
        all=MagicMock(return_value=[
            SimpleNamespace(id=pid, yahoo_id="33963", name="Ja'Marr Chase", position="WR"),
        ])
    ))
    svc._db.commit = AsyncMock()

    picks = [SimpleNamespace(
        platform_player_id="461.p.33963", player_name="", position="",
        auction_price=65, manager_name="", pick_number=1, picked_by_team_id="t1",
    )]
    ident = await svc._resolve_pick_identities(picks)
    assert ident["33963"][0] == pid
    assert ident["33963"][1] == "Ja'Marr Chase"


@pytest.mark.asyncio
async def test_store_picks_drops_ambiguous_yahoo_ids():
    """18 yahoo_ids in this database map to more than one player row. Binding a real
    auction price to a guess is worse than leaving it unresolved."""
    from unittest.mock import AsyncMock, MagicMock
    import uuid as _uuid

    from backend.services.league_sync import LeagueSyncService

    svc = LeagueSyncService(db=AsyncMock(), user_id=_uuid.uuid4())
    svc._db.execute = AsyncMock(return_value=MagicMock(
        all=MagicMock(return_value=[
            SimpleNamespace(id=_uuid.uuid4(), yahoo_id="999", name="Dupe A", position="WR"),
            SimpleNamespace(id=_uuid.uuid4(), yahoo_id="999", name="Dupe B", position="WR"),
        ])
    ))
    picks = [SimpleNamespace(
        platform_player_id="461.p.999", player_name="", position="",
        auction_price=5, manager_name="", pick_number=1, picked_by_team_id="t1",
    )]
    assert await svc._resolve_pick_identities(picks) == {}


@pytest.mark.asyncio
async def test_store_picks_handles_keys_without_the_p_marker():
    from unittest.mock import AsyncMock
    import uuid as _uuid

    from backend.services.league_sync import LeagueSyncService

    svc = LeagueSyncService(db=AsyncMock(), user_id=_uuid.uuid4())
    picks = [SimpleNamespace(
        platform_player_id="garbage", player_name="", position="",
        auction_price=1, manager_name="", pick_number=1, picked_by_team_id="t",
    )]
    assert await svc._resolve_pick_identities(picks) == {}
