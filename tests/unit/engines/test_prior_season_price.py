"""
Tests for market_value_historic — snapshot, API exposure, valuation agent context.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.engines.market_values import sync_market_values, _snapshot_current_market_values
from backend.models.market_value_historic import SOURCE_FANTASYPROS_CONSENSUS


# ---------------------------------------------------------------------------
# Snapshot — preserves current FP values before overwrite
# ---------------------------------------------------------------------------

def _scraped_rows(n, extra=None):
    """n scraped auction rows — enough to clear the minimum-rows guard in the sync."""
    rows = [
        {"name": f"Filler{i}", "position": "WR", "avg_value": 5.0,
         "min_value": None, "max_value": None}
        for i in range(n)
    ]
    return rows + list(extra or [])


@pytest.mark.asyncio
async def test_snapshot_runs_before_overwrite():
    """sync_market_values calls _snapshot_current_market_values before scraping."""
    fake_player = MagicMock()
    fake_player.name = "Patrick Mahomes"
    fake_player.position = "QB"
    fake_player.recommended_bid_ceiling = 30.0
    fake_player.market_value = Decimal("35")
    fake_player.market_value_fantasypros = Decimal("35")
    fake_player.market_value_confidence = "medium"
    fake_player.market_value_updated_at = None

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [fake_player]
    mock_result.all.return_value = []       # snapshot query returns no rows
    mock_result.scalar_one.return_value = 0  # nothing priced yet
    mock_session.execute.return_value = mock_result

    scraped = _scraped_rows(120, [
        {"name": "Patrick Mahomes", "position": "QB", "avg_value": 40.0,
         "min_value": 35, "max_value": 45},
    ])

    with patch(
        "backend.engines.market_values._scrape_in_thread",
        return_value=(scraped, 2026, True),
    ), patch(
        "backend.engines.market_values._store_metadata",
        new_callable=AsyncMock,
    ), patch(
        "backend.engines.market_values._snapshot_current_market_values",
        new_callable=AsyncMock,
        return_value=0,
    ) as mock_snapshot:
        result = await sync_market_values(mock_session)

    # Snapshot was called
    mock_snapshot.assert_awaited_once_with(mock_session)
    # The 120 filler names match no player row; Mahomes does.
    assert result["matched"] == 1
    assert fake_player.market_value_fantasypros == 40.0


@pytest.mark.asyncio
async def test_snapshot_skipped_on_dry_run():
    """Dry run does not snapshot."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_result.all.return_value = []
    mock_session.execute.return_value = mock_result

    with patch(
        "backend.engines.market_values._scrape_in_thread",
        return_value=([], 2026, True),
    ), patch(
        "backend.engines.market_values._snapshot_current_market_values",
        new_callable=AsyncMock,
    ) as mock_snapshot:
        await sync_market_values(mock_session, dry_run=True)

    mock_snapshot.assert_not_awaited()


def _priced_row(price, updated_at, pid="fake-uuid"):
    row = MagicMock()
    row.id = pid
    row.market_value_fantasypros = Decimal(str(price))
    row.market_value_updated_at = updated_at
    return row


@pytest.mark.asyncio
async def test_snapshot_labels_each_price_with_the_season_it_was_scraped_in():
    """The archived season comes from the price's OWN timestamp, not today's clock.

    The snapshot deliberately runs BEFORE the scrape, so the column still holds the
    PREVIOUS run's price. Stamping get_current_season() therefore mislabelled every
    carried-over price at each season boundary: a price scraped in one season and
    archived by the first run after the following March was recorded under the later
    year. Both readers of the table look for get_current_season() - 1, so a row
    written that way is invisible for a year and then served as the wrong season's
    price.
    """
    from backend.engines.market_values import _snapshot_payload

    # Two prices scraped in DIFFERENT seasons, archived by one run. A February
    # timestamp belongs to the PREVIOUS season — the league year turns over in March.
    rows = [
        _priced_row(40, datetime(2025, 8, 1, tzinfo=timezone.utc), "scraped-in-2025"),
        _priced_row(50, datetime(2026, 8, 1, tzinfo=timezone.utc), "scraped-in-2026"),
        _priced_row(60, datetime(2026, 2, 1, tzinfo=timezone.utc), "scraped-in-feb"),
    ]
    payload, skipped = _snapshot_payload(rows)

    assert skipped == 0
    seasons = {p["player_id"]: p["season_year"] for p in payload}
    assert seasons == {
        "scraped-in-2025": 2025,
        "scraped-in-2026": 2026,
        "scraped-in-feb": 2025,
    }
    # Everything this function writes is a consensus estimate, never a realized price.
    assert {p["source"] for p in payload} == {SOURCE_FANTASYPROS_CONSENSUS}


@pytest.mark.asyncio
async def test_snapshot_skips_a_price_with_no_timestamp():
    """A price with no timestamp cannot be assigned to a season, so it is not archived.

    That is the marker the as-of market seeder leaves: it fills this column with a PAST
    season's realized auction prices and sets no timestamp. Without the skip, the next
    ordinary sync would read those real prices out of the column and file them in the
    historic table under the PRESENT season, where the backtest would later score
    against them as if they were that season's market.
    """
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = [_priced_row(40, None)]
    mock_session.execute.return_value = mock_result

    count = await _snapshot_current_market_values(mock_session)

    assert count == 0
    # SELECT only — no INSERT was issued.
    assert mock_session.execute.await_count == 1
    mock_session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_returns_zero_when_no_players():
    """Snapshot returns 0 when no players have FP values."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.execute.return_value = mock_result

    with patch("backend.engines.market_values.get_current_season", return_value=2026):
        count = await _snapshot_current_market_values(mock_session)

    assert count == 0
    # Only SELECT, no INSERT
    assert mock_session.execute.call_count == 1


# ---------------------------------------------------------------------------
# The prior-season rotation was REMOVED, deliberately.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_no_longer_rotates_a_same_season_price_into_prior_season():
    """The sync must not write market_value_prior_season.

    It used to move the outgoing price into that column and label it
    ``year_used - 1``. Two things were wrong with that. The value is not last
    season's price — it is the previous SCRAPE of the current season, often days
    old, so the label was a different quantity from the number. And the rotation only
    fired when the price had CHANGED, so the stored value carried an arbitrary
    per-player date. Adding the market refresh to the pre-draft pipeline would have
    run this on every pipeline pass, making the column drift further with each run.

    The column has no readers anywhere in the application — consistent with it being
    NULL on every row of the development database — so the contradiction is resolved
    by deleting the write rather than by correcting the label.
    seed_prior_season_from_auction_history, which populates it from real auction
    history, is untouched.
    """
    fake_player = MagicMock()
    fake_player.name = "Patrick Mahomes"
    fake_player.position = "QB"
    fake_player.recommended_bid_ceiling = 30.0
    fake_player.market_value = Decimal("35")
    fake_player.market_value_fantasypros = Decimal("35")
    fake_player.market_value_prior_season = None
    fake_player.market_value_prior_season_year = None
    fake_player.market_value_confidence = "medium"
    fake_player.market_value_updated_at = None

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [fake_player]
    mock_result.all.return_value = []
    mock_result.scalar_one.return_value = 0
    mock_session.execute.return_value = mock_result

    scraped = _scraped_rows(120, [
        {"name": "Patrick Mahomes", "position": "QB", "avg_value": 40.0,
         "min_value": 35, "max_value": 45},
    ])

    with patch(
        "backend.engines.market_values._scrape_in_thread",
        return_value=(scraped, 2026, True),
    ), patch(
        "backend.engines.market_values._store_metadata",
        new_callable=AsyncMock,
    ), patch(
        "backend.engines.market_values._snapshot_current_market_values",
        new_callable=AsyncMock,
        return_value=0,
    ):
        result = await sync_market_values(mock_session)

    assert result["matched"] == 1
    assert fake_player.market_value_fantasypros == 40.0
    assert fake_player.market_value_prior_season is None
    assert fake_player.market_value_prior_season_year is None


# ---------------------------------------------------------------------------
# Valuation agent context is MARKET-BLIND (ToS): market_value_fantasypros and
# prior_season_price are stripped from _build_player_context on every path — even
# when they are available — so the blind price opinion never sees market. Market
# re-enters only in the deterministic post-pass (reconcile_value_signals).
# ---------------------------------------------------------------------------

def test_valuation_agent_context_excludes_market_even_when_available():
    """_build_player_context must NOT include market_value_fantasypros or
    prior_season_price, even for a player that HAS both."""
    from backend.agents.valuation_agent import ValuationAgent

    agent = ValuationAgent.__new__(ValuationAgent)

    # Mock historic price record (available — must still be excluded)
    hist = MagicMock()
    hist.season_year = 2025
    hist.price = Decimal("42")

    player = MagicMock()
    player.name = "CeeDee Lamb"
    player.position = "WR"
    player.team_abbr = "DAL"
    player.age = 26
    player.tier = 1
    player.is_rookie = False
    player.recommended_bid_ceiling = Decimal("55")
    player.baseline_value = Decimal("50")
    player.market_value = Decimal("48")
    player.value_gap = Decimal("2")
    player.value_gap_signal = "aligned"
    player.ceiling_value = Decimal("60")
    player.floor_value = Decimal("35")
    player.market_value_fantasypros = Decimal("48")
    player.historic_prices = [hist]
    player.profile = None
    player.injury_profile = None
    player.schedule = None
    player.dependencies = []

    with patch("backend.agents.valuation_agent.get_current_season", return_value=2026):
        ctx = agent._build_player_context(player)

    for k in ("market_value", "value_gap", "value_gap_signal",
              "market_value_fantasypros", "prior_season_price"):
        assert k not in ctx, f"{k} must be stripped from the blind PPR context"
    # The non-market math anchor is still present.
    assert ctx["math_bid_ceiling"] == 55.0


def test_valuation_prompt_no_league_language():
    """System prompt must not use 'your league' except in NEVER/forbidden instructions."""
    from backend.agents.valuation_agent import SYSTEM_PROMPT

    forbidden = ["your league paid", "your league values", "in your league"]
    for line in SYSTEM_PROMPT.splitlines():
        stripped = line.strip().lower()
        # Skip lines that are instructions about what NOT to say
        if "never" in stripped or "forbidden" in stripped or "correct" in stripped:
            continue
        for phrase in forbidden:
            assert phrase not in stripped, (
                f"Found forbidden phrase '{phrase}' in non-instruction line: {line.strip()}"
            )
