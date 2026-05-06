"""
FantasyPros integration — auction values and ADP via Playwright.

FantasyPros uses JavaScript-rendered DataTables so we need a real browser.
Playwright launches headless Chromium, waits for the table, and parses it.

Scoring formats: 'ppr' | 'half_ppr' | 'standard'

Auction values are scraped from the DraftWizard calculator endpoint
(the old /auction-values/ppr.php URLs were removed by FantasyPros).
ADP is scraped from /nfl/adp/ pages which support ?year= for historical data.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger(__name__)

FP_BASE = "https://www.fantasypros.com/nfl"

# DraftWizard calculator — serves the actual auction dollar values.
# Supports ?scoring=PPR|HALF|STD and ?teams=N
# NOTE: ?year= param is accepted but ignored by DraftWizard — it always
# returns current projections. This is fine for draft prep (we want current).
AUCTION_URL = "https://draftwizard.fantasypros.com/auction/fp_nfl.jsp"

SCORING_PARAMS: dict[str, str] = {
    "ppr":      "PPR",
    "half_ppr": "HALF",
    "standard": "STD",
}

ADP_URLS: dict[str, str] = {
    "ppr":      f"{FP_BASE}/adp/ppr-overall.php",
    "half_ppr": f"{FP_BASE}/adp/half-point-ppr-overall.php",
    "standard": f"{FP_BASE}/adp/overall.php",
}


def _clean_dollar(value: str) -> Optional[float]:
    try:
        return float(Decimal(value.replace("$", "").replace(",", "").strip()))
    except (InvalidOperation, ValueError):
        return None


def _clean_float(value: str) -> Optional[float]:
    try:
        return float(value.strip())
    except ValueError:
        return None


async def get_auction_values(
    scoring_format: str = "ppr",
    year: int | None = None,
    teams: int = 12,
) -> list[dict]:
    """
    Scrape FantasyPros auction values from the DraftWizard calculator.

    Args:
        scoring_format: 'ppr' | 'half_ppr' | 'standard'
        year: Passed to URL but DraftWizard currently ignores it
              (always returns current projections). Kept for API
              compatibility and future support.
        teams: Number of teams in league (affects dollar scaling).

    Returns a list of dicts:
      {name, team, position, avg_value, min_value, max_value, scoring_format}
    """
    from playwright.async_api import async_playwright

    scoring_param = SCORING_PARAMS.get(scoring_format)
    if not scoring_param:
        raise ValueError(
            f"Unknown scoring format '{scoring_format}'. Use: {list(SCORING_PARAMS)}"
        )

    url = f"{AUCTION_URL}?scoring={scoring_param}&teams={teams}"
    if year is not None:
        url += f"&year={year}"

    logger.info(
        "Fetching FantasyPros auction values (%s, %d teams) from %s",
        scoring_format, teams, url,
    )

    players: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # DraftWizard renders tables with class .ValueTable
            # Table 0 = Overall (all positions combined) — that's the one we want
            # Use state="attached" — tables are in the DOM but may not be
            # "visible" (off-screen or initially collapsed).
            await page.wait_for_selector(".ValueTable", state="attached", timeout=20_000)
            await page.wait_for_timeout(2_000)

            tables = await page.query_selector_all(".ValueTable")
            if not tables:
                logger.warning("No .ValueTable found on DraftWizard page")
                return players

            # Use the first table (Overall, all positions)
            rows = await tables[0].query_selector_all("tbody tr")

            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 3:
                    continue

                # Col layout: Rank | Player (TEAM - POS) | $Value | RawValue
                player_text = (await cells[1].inner_text()).strip()
                value_raw = (await cells[2].inner_text()).strip()

                # Parse "Puka Nacua (LAR - WR)" format
                name, team, position = _parse_player_cell(player_text)
                avg_value = _clean_dollar(value_raw)

                if not name or avg_value is None:
                    continue

                players.append({
                    "name":           name,
                    "team":           team,
                    "position":       position,
                    "avg_value":      avg_value,
                    "min_value":      None,  # DraftWizard doesn't show min/max
                    "max_value":      None,
                    "scoring_format": scoring_format,
                })

        finally:
            await browser.close()

    logger.info("Retrieved %d players from FantasyPros auction values", len(players))
    return players


def _parse_player_cell(text: str) -> tuple[str, str, str]:
    """
    Parse player cell text like 'Puka Nacua (LAR - WR)' or 'Josh Allen, BUF'.

    Returns (name, team, position).
    """
    # Format 1: "Name (TEAM - POS)" — overall table
    if "(" in text and ")" in text:
        paren_start = text.rindex("(")
        name = text[:paren_start].strip()
        meta = text[paren_start + 1 : text.rindex(")")].strip()
        parts = [p.strip() for p in meta.replace("-", " ").split()]
        position = next(
            (p for p in parts if p in {"QB", "RB", "WR", "TE", "K", "DST", "DEF"}),
            "",
        )
        team = next(
            (p for p in parts if p not in {"QB", "RB", "WR", "TE", "K", "DST", "DEF"} and len(p) <= 4),
            "",
        )
        return name, team, position

    # Format 2: "Name, TEAM" — positional tables
    if "," in text:
        parts = text.rsplit(",", 1)
        name = parts[0].strip()
        team = parts[1].strip() if len(parts) > 1 else ""
        return name, team, ""

    return text.strip(), "", ""


async def get_adp(
    scoring_format: str = "half_ppr",
    year: int | None = None,
) -> list[dict]:
    """
    Scrape FantasyPros ADP for the given scoring format.

    Args:
        scoring_format: 'ppr' | 'half_ppr' | 'standard'
        year: If provided, appends ?year=YYYY to fetch historical data.
              ADP pages support historical years (verified).

    Returns a list of dicts:
      {rank, name, team, position, bye, adp, best, worst, scoring_format}
    """
    from playwright.async_api import async_playwright

    url = ADP_URLS.get(scoring_format)
    if not url:
        raise ValueError(f"Unknown scoring format '{scoring_format}'. Use: {list(ADP_URLS)}")

    if year is not None:
        url += f"?year={year}"

    logger.info("Fetching FantasyPros ADP (%s) from %s", scoring_format, url)

    players: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_selector("table#data", timeout=20_000)

            show_all = page.locator("select[name='data_length'] option[value='-1']")
            if await show_all.count() > 0:
                await page.select_option("select[name='data_length']", value="-1")
                await page.wait_for_timeout(1_500)

            rows = await page.query_selector_all("table#data tbody tr")

            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 5:
                    continue

                rank_raw = (await cells[0].inner_text()).strip()

                player_cell = cells[1]
                name_el = await player_cell.query_selector("a")
                name = (await name_el.inner_text()).strip() if name_el else (await player_cell.inner_text()).strip()

                team     = (await cells[2].inner_text()).strip() if len(cells) > 2 else ""
                position = (await cells[3].inner_text()).strip() if len(cells) > 3 else ""
                bye_raw  = (await cells[4].inner_text()).strip() if len(cells) > 4 else ""
                adp_raw  = (await cells[5].inner_text()).strip() if len(cells) > 5 else ""
                best_raw = (await cells[7].inner_text()).strip() if len(cells) > 7 else ""
                worst_raw = (await cells[8].inner_text()).strip() if len(cells) > 8 else ""

                players.append({
                    "rank":           _clean_float(rank_raw),
                    "name":           name,
                    "team":           team,
                    "position":       position,
                    "bye":            _clean_float(bye_raw),
                    "adp":            _clean_float(adp_raw),
                    "best":           _clean_float(best_raw),
                    "worst":          _clean_float(worst_raw),
                    "scoring_format": scoring_format,
                })

        finally:
            await browser.close()

    logger.info("Retrieved %d players from FantasyPros ADP", len(players))
    return players


async def get_market_values(
    scoring_format: str = "ppr",
    year: int | None = None,
    teams: int = 12,
) -> dict[str, dict]:
    """
    Fetch auction values (and optionally ADP), merge by name,
    return keyed by player name. Primary entry point for market_value fields.

    Args:
        scoring_format: 'ppr' | 'half_ppr' | 'standard'
        year: Passed through to scrapers.
        teams: Number of league teams (affects auction dollar scaling).
    """
    auction = await get_auction_values(scoring_format, year=year, teams=teams)

    merged: dict[str, dict] = {}
    for p in auction:
        merged[p["name"]] = {
            "name":              p["name"],
            "team":              p["team"],
            "position":          p["position"],
            "auction_value":     p["avg_value"],
            "auction_min":       p["min_value"],
            "auction_max":       p["max_value"],
            "scoring_format":    scoring_format,
        }

    return merged


async def get_best_auction_values(
    format: str = "ppr",
    teams: int = 12,
) -> tuple[list[dict], int, bool]:
    """
    Get auction values using the best available year,
    with automatic fallback to previous season.

    Returns:
        (values, year_used, is_current_season)
    """
    from backend.utils.seasons import get_best_available_auction_year

    async def _scraper(fmt: str, yr: int) -> list[dict]:
        return await get_auction_values(fmt, year=yr, teams=teams)

    return await get_best_available_auction_year(
        scraper_fn=_scraper,
        format=format,
    )
