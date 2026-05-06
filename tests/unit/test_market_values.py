"""
tests/unit/test_market_values.py

Tests for market value year resolution, fallback logic, and sync engine.
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
# get_fantasypros_auction_year()
# ---------------------------------------------------------------------------

def test_fantasypros_year_before_july_returns_current_season():
    """In months 1-6: returns current_season (last completed season)."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 3, 15)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2025  # current_season=2025 in March 2026
        assert is_current is False


def test_fantasypros_year_may_returns_current_season():
    """May — current_season is 2025, FP data not yet refreshed for 2026."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 5, 6)
        year, is_current = get_fantasypros_auction_year()
        assert year == 2025
        assert is_current is False


def test_fantasypros_year_june_returns_current_season():
    """June — current_season flips to 2026, but FP data not ready until July."""
    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 6, 30)
        year, is_current = get_fantasypros_auction_year()
        # current_season in June = 2026, month < 7, so is_current=False
        assert year == 2026
        assert is_current is False


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


# ---------------------------------------------------------------------------
# get_best_available_auction_year()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_when_current_year_insufficient():
    """Fewer than 100 players returned → falls back to previous year."""
    call_log = []

    async def mock_scraper(fmt, yr):
        call_log.append(yr)
        # May 2026: current_season=2025, preferred=2025
        if yr == 2025:
            return [{"name": f"p{i}"} for i in range(50)]  # too few
        else:
            return [{"name": f"p{i}"} for i in range(200)]  # fallback (2024)

    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 5, 1)
        values, year, is_current = await get_best_available_auction_year(
            mock_scraper, format="ppr"
        )

    assert len(values) == 200
    assert is_current is False
    # Should have tried preferred year (2025) first, then fallback (2024)
    assert len(call_log) == 2
    assert call_log == [2025, 2024]


@pytest.mark.asyncio
async def test_no_fallback_when_current_year_sufficient():
    """100+ players returned → uses preferred year directly."""
    async def mock_scraper(fmt, yr):
        return [{"name": f"p{i}"} for i in range(200)]

    with patch("backend.utils.seasons.date") as mock_date:
        mock_date.today.return_value = date(2026, 8, 1)
        values, year, is_current = await get_best_available_auction_year(
            mock_scraper, format="ppr"
        )

    assert len(values) == 200
    assert year == 2026
    assert is_current is True


@pytest.mark.asyncio
async def test_fallback_when_preferred_year_errors():
    """If preferred year raises exception, falls back to previous."""
    async def mock_scraper(fmt, yr):
        if yr == 2025:
            raise RuntimeError("scrape failed")
        return [{"name": f"p{i}"} for i in range(150)]

    with patch("backend.utils.seasons.date") as mock_date:
        # May 2026: current_season=2025, preferred=2025 → error → fallback=2024
        mock_date.today.return_value = date(2026, 5, 1)
        values, year, is_current = await get_best_available_auction_year(
            mock_scraper, format="ppr"
        )

    assert len(values) == 150
    assert year == 2024
    assert is_current is False


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

@pytest.mark.asyncio
async def test_sync_market_values_returns_year_info():
    """sync_market_values result includes year and is_current_season."""
    from backend.engines.market_values import sync_market_values

    # Provide some player data so it doesn't hit the "empty" branch
    fake_values = [{"name": "Test Player", "avg_value": 10.0, "min_value": None, "max_value": None}]

    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()

    with patch("asyncio.get_running_loop") as mock_loop:
        mock_loop.return_value.run_in_executor = AsyncMock(
            return_value=(fake_values, 2025, False)
        )

        result = await sync_market_values(session, scoring_format="ppr")

    assert result["year"] == 2025
    assert result["is_current_season"] is False
    # Player won't match (empty DB) so it goes to unmatched
    assert result["unmatched"] == 1
