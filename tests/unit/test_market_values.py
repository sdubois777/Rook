"""
tests/unit/test_market_values.py

Tests for market value year resolution and sync engine.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.utils.seasons import (
    get_fantasypros_auction_year,
    get_best_available_auction_year,
)


# ---------------------------------------------------------------------------
# get_fantasypros_auction_year() — always returns current season
# ---------------------------------------------------------------------------

def test_fantasypros_year_march():
    """March — current_season=2026, always is_current=True."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 3, 15)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2026
        assert is_current is True


def test_fantasypros_year_may():
    """May — current_season=2026, always is_current=True."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 5, 6)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2026
        assert is_current is True


def test_fantasypros_year_july_returns_current():
    """In months 7-12: returns current_season."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 7, 15)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2026
        assert is_current is True


def test_fantasypros_year_august_returns_current():
    """August — peak draft prep season — uses current."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 8, 20)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2026
        assert is_current is True


def test_fantasypros_year_december_returns_current():
    """December — still current season."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 12, 1)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2026
        assert is_current is True


def test_fantasypros_year_january():
    """January — current_season=2025 (playoffs), still is_current=True."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 1, 15)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2025
        assert is_current is True


# ---------------------------------------------------------------------------
# get_best_available_auction_year() — no fallback, always current season
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_best_available_returns_current_year():
    """Returns current season year with is_current=True."""
    async def mock_scraper(fmt, yr):
        return [{"name": f"p{i}"} for i in range(200)]

    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 5, 1)
        values, year, is_current = await get_best_available_auction_year(
            mock_scraper, format="ppr"
        )

    assert len(values) == 200
    assert year == 2026
    assert is_current is True


@pytest.mark.asyncio
async def test_best_available_low_count_still_returns():
    """Even with fewer than 100 results, still returns current year (no fallback)."""
    async def mock_scraper(fmt, yr):
        return [{"name": f"p{i}"} for i in range(50)]

    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 5, 1)
        values, year, is_current = await get_best_available_auction_year(
            mock_scraper, format="ppr"
        )

    assert len(values) == 50
    assert year == 2026
    assert is_current is True


@pytest.mark.asyncio
async def test_best_available_error_propagates():
    """Scraper errors propagate (no silent fallback to wrong year)."""
    async def mock_scraper(fmt, yr):
        raise RuntimeError("scrape failed")

    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 5, 1)
        with pytest.raises(RuntimeError, match="scrape failed"):
            await get_best_available_auction_year(mock_scraper, format="ppr")


# ---------------------------------------------------------------------------
# No hardcoded years in market value modules
# ---------------------------------------------------------------------------

def test_no_hardcoded_years_in_market_values_engine():
    """No hardcoded years in backend/engines/market_values.py."""
    import re
    from pathlib import Path

    path = Path(__file__).parent.parent.parent / "backend" / "engines" / "market_values.py"
    content = path.read_text(encoding="utf-8")
    year_pattern = re.compile(r"\b(202[2-9])\b")

    violations = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        if year_pattern.search(line):
            violations.append(f"market_values.py:{lineno}: {line.strip()}")

    assert not violations, (
        "Hardcoded years found:\n" + "\n".join(violations)
    )


def test_no_hardcoded_years_in_refresh_script():
    """No hardcoded years in scripts/refresh_market_values.py."""
    import re
    from pathlib import Path

    path = Path(__file__).parent.parent.parent / "scripts" / "refresh_market_values.py"
    content = path.read_text(encoding="utf-8")
    year_pattern = re.compile(r"\b(202[2-9])\b")

    violations = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        if year_pattern.search(line):
            violations.append(f"refresh_market_values.py:{lineno}: {line.strip()}")

    assert not violations, (
        "Hardcoded years found:\n" + "\n".join(violations)
    )


def test_no_hardcoded_years_in_fantasypros_module():
    """No hardcoded years in backend/integrations/fantasypros.py."""
    import re
    from pathlib import Path

    path = Path(__file__).parent.parent.parent / "backend" / "integrations" / "fantasypros.py"
    content = path.read_text(encoding="utf-8")
    year_pattern = re.compile(r"\b(202[2-9])\b")

    violations = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        if year_pattern.search(line):
            violations.append(f"fantasypros.py:{lineno}: {line.strip()}")

    assert not violations, (
        "Hardcoded years found:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Sync engine (mocked scraper)
# ---------------------------------------------------------------------------

def _sync_session(db_players=None, previous_scrape=None):
    """AsyncSession double for sync_market_values.

    One result object answers every access the engine makes: .scalar_one_or_none() for
    the previous refresh's matched count, .scalars().all() for the player load, and
    .all() for the archive step's row list.
    """
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = db_players or []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    result_mock.scalar_one_or_none.return_value = previous_scrape
    result_mock.all.return_value = []          # archive step: nothing to snapshot
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _scraped(n, prefix="Player"):
    """n scraped auction rows — enough to clear the minimum-rows guard."""
    return [
        {"name": f"{prefix}{i}", "avg_value": 10.0, "position": "WR",
         "min_value": None, "max_value": None}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_sync_market_values_returns_year_info():
    """sync_market_values result includes year and is_current_season."""
    from backend.engines.market_values import sync_market_values

    fake_values = _scraped(120)
    session = _sync_session()

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            return_value=(fake_values, 2026, True)
        )
        result = await sync_market_values(session, scoring_format="ppr")

    assert result["year"] == 2026
    assert result["is_current_season"] is True
    # No players in the DB, so every scraped row goes to unmatched.
    assert result["unmatched"] == 120


@pytest.mark.asyncio
async def test_a_short_scrape_is_refused_and_writes_nothing():
    """A truncated scrape must not overwrite the board and must not report success.

    The DraftWizard page is parsed after a fixed wait and rows with too few cells are
    dropped, so a half-rendered table returns a SHORT list, not an empty one. The only
    previous guard was strict emptiness, so a five-row scrape updated five players,
    committed, recorded a healthy refresh, and left the rest of the board at whatever
    age it already had with nothing saying so.
    """
    from backend.engines.market_values import sync_market_values

    session = _sync_session(previous_scrape=300)

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            return_value=(_scraped(5), 2026, True)
        )
        result = await sync_market_values(session, scoring_format="ppr")

    assert result["matched"] == 0
    assert "Refused" in result["error"]
    session.commit.assert_not_awaited()
    session.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_a_scrape_that_collapses_against_the_previous_one_is_refused():
    """120 rows clears the absolute floor but not 80% of the previous 300."""
    from backend.engines.market_values import sync_market_values

    session = _sync_session(previous_scrape=300)

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            return_value=(_scraped(120), 2026, True)
        )
        result = await sync_market_values(session, scoring_format="ppr")

    assert result["matched"] == 0
    assert "Refused" in result["error"]
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_refuses_to_run_under_an_asof_clock():
    """A live scrape on a past-dated board would overwrite that season's real prices.

    The pipeline stage already skips, but the manual script and the API endpoint call
    straight into the engine, so the refusal belongs here where all three inherit it.
    """
    from backend.engines.market_values import sync_market_values

    session = _sync_session()

    with patch("backend.engines.market_values.asof_active", return_value=True):
        result = await sync_market_values(session, scoring_format="ppr")

    assert result["matched"] == 0
    assert "ROOK_ASOF_DATE" in result["error"]
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Matching a scraped row to a player row.
#
# The DraftWizard feed carries only name, team and position, so the id-first chain
# CLAUDE.md rule 7 requires is not available. Position is, and it is what separates a
# genuine duplicate from two different people who share a name. The matcher this
# replaced was a plain dict keyed on the normalized name: assignment, so one row per
# collision was silently lost, and position was never consulted.
#
# Both cases below were measured on the development database before the change.
# ---------------------------------------------------------------------------

def _row(name, position="WR", value=10.0, team=None):
    return {"name": name, "position": position, "avg_value": value, "team": team}


def _db_row(name, position, ceiling=None, team=None):
    p = MagicMock()
    p.id = f"id-{name}-{position}"
    p.name = name
    p.position = position
    p.team_abbr = team
    p.recommended_bid_ceiling = ceiling
    return p


def _index(players):
    from backend.engines.market_values import normalize_player_name
    idx = {}
    for p in players:
        idx.setdefault(normalize_player_name(p.name), []).append(p)
    return idx


def test_position_decides_between_two_players_sharing_a_name():
    """Antonio Williams: the price went to a team-less running back row while the
    real receiver on Washington kept no price at all — which removes him from the
    board entirely, because draftable_filter gates on that column."""
    from backend.engines.market_values import _resolve_scraped_player

    receiver = _db_row("Antonio Williams", "WR", ceiling=1.0, team="WAS")
    shell_rb = _db_row("Antonio Williams", "RB")

    player, reason = _resolve_scraped_player(
        _row("Antonio Williams", "WR"), _index([shell_rb, receiver])
    )
    assert reason == "ok"
    assert player is receiver


def test_a_valued_row_wins_over_a_leftover_duplicate_at_the_same_position():
    """Kenneth Walker: the later run's price landed on a team-less receiver row while
    the real running back on Kansas City kept a price six days older. When position
    cannot separate them, prefer the row the board actually renders — a valued row
    carries a bid ceiling, a leftover shell row does not."""
    from backend.engines.market_values import _resolve_scraped_player

    real = _db_row("Kenneth Walker", "RB", ceiling=19.82, team="KC")
    shell = _db_row("Kenneth Walker", "RB")

    player, reason = _resolve_scraped_player(
        _row("Kenneth Walker", "RB"), _index([shell, real])
    )
    assert reason == "ok"
    assert player is real


def test_a_genuinely_ambiguous_name_is_skipped_not_guessed():
    """Two rows equally answering to one name get NO price. A missing price is visible
    and self-correcting; a price on the wrong player is neither."""
    from backend.engines.market_values import _resolve_scraped_player

    a = _db_row("Frank Gore", "RB", ceiling=5.0)
    b = _db_row("Frank Gore", "RB", ceiling=5.0)

    player, reason = _resolve_scraped_player(_row("Frank Gore", "RB"), _index([a, b]))
    assert player is None
    assert reason == "ambiguous"


def test_an_unknown_name_is_unmatched_not_ambiguous():
    from backend.engines.market_values import _resolve_scraped_player

    player, reason = _resolve_scraped_player(
        _row("Nobody At All", "WR"), _index([_db_row("Someone Else", "WR")])
    )
    assert player is None
    assert reason == "unmatched"


@pytest.mark.asyncio
async def test_ambiguous_names_are_reported_separately_from_unmatched():
    """An ambiguous name matched and was skipped on purpose. Folding it into
    'unmatched' would hide the duplicate-row problem that caused it."""
    from backend.engines.market_values import sync_market_values

    twins = [_db_row("Frank Gore", "RB", ceiling=5.0),
             _db_row("Frank Gore", "RB", ceiling=5.0)]
    session = _sync_session(db_players=twins)

    values = _scraped(120) + [_row("Frank Gore", "RB")]
    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            return_value=(values, 2026, True)
        )
        result = await sync_market_values(session, scoring_format="ppr")

    assert result["ambiguous"] == 1
    assert "Frank Gore" in result["ambiguous_names"]
    assert result["matched"] == 0


@pytest.mark.asyncio
async def test_the_scrape_floor_does_not_grow_with_accumulated_priced_rows():
    """The collapse check must compare against the PREVIOUS SCRAPE, not against how
    many players currently hold a price.

    Nothing in the codebase ever clears a price, so the count of priced rows is the
    union of every scrape ever run and only grows. A floor derived from it climbs past
    the real scrape size and then refuses every healthy run — the guard becomes a
    permanent outage instead of a safety check. This test pins a realistic case: 357
    rows is what the live feed returns, and it must pass even when far more rows carry
    a price from earlier runs.
    """
    from backend.engines.market_values import sync_market_values

    session = _sync_session(previous_scrape=357)
    # 900 players already hold a price from the accumulated history of past scrapes.
    session.execute.return_value.scalars.return_value.all.return_value = []

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            return_value=(_scraped(357), 2026, True)
        )
        result = await sync_market_values(session, scoring_format="ppr")

    assert "error" not in result, result.get("error")
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_a_first_ever_sync_is_not_blocked_by_the_relative_floor():
    """With no previous refresh recorded, only the absolute floor applies."""
    from backend.engines.market_values import sync_market_values

    session = _sync_session(previous_scrape=None)

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            return_value=(_scraped(150), 2026, True)
        )
        result = await sync_market_values(session, scoring_format="ppr")

    assert "error" not in result, result.get("error")
    session.commit.assert_awaited()


def test_a_name_matching_several_rows_at_the_wrong_position_is_skipped():
    """When several rows share a name and NONE plays the scraped position, price none.

    Falling through to the bid-ceiling tiebreak here would hand the price to a player
    at a different position, which is the failure the position check exists to stop.
    """
    from backend.engines.market_values import _resolve_scraped_player

    a = _db_row("Josh Allen", "LB", ceiling=None)
    b = _db_row("Josh Allen", "DE", ceiling=4.0)

    player, reason = _resolve_scraped_player(_row("Josh Allen", "QB"), _index([a, b]))
    assert player is None
    assert reason == "ambiguous"


def test_team_separates_two_different_people_with_the_same_name_and_position():
    """Frank Gore Sr and Frank Gore Jr are different humans at the same position.

    Both are stored as "Frank Gore" in this database, and the name normalizer strips
    the generational suffix, so the feed's "Frank Gore Jr." collides with both.
    CLAUDE.md names this exact pair as a case that must never be resolved on a name
    key. The auction feed carries a team, and theirs differ: the active player is on
    Buffalo, the retired one has no team.
    """
    from backend.engines.market_values import _resolve_scraped_player

    active = _db_row("Frank Gore", "RB", team="BUF")
    retired = _db_row("Frank Gore", "RB", team=None)

    player, reason = _resolve_scraped_player(
        _row("Frank Gore Jr.", "RB", team="BUF"), _index([retired, active])
    )
    assert reason == "ok"
    assert player is active


def test_team_is_a_hint_not_a_constraint():
    """A team matching no candidate must narrow nothing, not reject the row.

    A player who changed teams recently can carry a stale team in either source, so a
    team mismatch is much weaker evidence than a position mismatch.
    """
    from backend.engines.market_values import _resolve_scraped_player

    valued = _db_row("Some Player", "RB", ceiling=12.0, team="KC")
    shell = _db_row("Some Player", "RB", team=None)

    # Scraped team matches neither row; the valued-row tiebreak still resolves it.
    player, reason = _resolve_scraped_player(
        _row("Some Player", "RB", team="NYJ"), _index([shell, valued])
    )
    assert reason == "ok"
    assert player is valued
