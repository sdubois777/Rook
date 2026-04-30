"""
OverTheCap integration — roster, transaction, and contract data.

OTC doesn't have a public API. We scrape HTML tables with httpx + BeautifulSoup.
nfl_data_py.import_contracts() also sources from OTC and is used where it covers
what we need. Direct scraping fills the gaps (transactions, depth charts).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from bs4 import BeautifulSoup
import pandas as pd

logger = logging.getLogger(__name__)

OTC_BASE = "https://overthecap.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Map nfl_data_py team abbreviations → OTC team slug
TEAM_SLUGS: dict[str, str] = {
    "ARI": "arizona-cardinals",
    "ATL": "atlanta-falcons",
    "BAL": "baltimore-ravens",
    "BUF": "buffalo-bills",
    "CAR": "carolina-panthers",
    "CHI": "chicago-bears",
    "CIN": "cincinnati-bengals",
    "CLE": "cleveland-browns",
    "DAL": "dallas-cowboys",
    "DEN": "denver-broncos",
    "DET": "detroit-lions",
    "GB":  "green-bay-packers",
    "HOU": "houston-texans",
    "IND": "indianapolis-colts",
    "JAX": "jacksonville-jaguars",
    "KC":  "kansas-city-chiefs",
    "LA":  "los-angeles-rams",
    "LAC": "los-angeles-chargers",
    "LV":  "las-vegas-raiders",
    "MIA": "miami-dolphins",
    "MIN": "minnesota-vikings",
    "NE":  "new-england-patriots",
    "NO":  "new-orleans-saints",
    "NYG": "new-york-giants",
    "NYJ": "new-york-jets",
    "PHI": "philadelphia-eagles",
    "PIT": "pittsburgh-steelers",
    "SEA": "seattle-seahawks",
    "SF":  "san-francisco-49ers",
    "TB":  "tampa-bay-buccaneers",
    "TEN": "tennessee-titans",
    "WAS": "washington-commanders",
}


async def _fetch_html(url: str, timeout: int = 30) -> str:
    async with httpx.AsyncClient(headers=HEADERS, timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


def _parse_table(soup: BeautifulSoup, table_selector: str = "table") -> list[dict]:
    """Generic table parser — returns list of row dicts keyed by header text."""
    table = soup.select_one(table_selector)
    if not table:
        return []

    headers = [th.get_text(strip=True) for th in table.select("thead th, thead td")]
    if not headers:
        headers = [th.get_text(strip=True) for th in table.select("tr:first-child th, tr:first-child td")]

    rows = []
    for tr in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if not cells or all(c == "" for c in cells):
            continue
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
        else:
            # Fall back to positional keys
            rows.append({f"col_{i}": v for i, v in enumerate(cells)})

    return rows


async def get_transactions(year: int) -> list[dict]:
    """
    Scrape all transactions for a given year from OverTheCap.
    Returns a list of dicts with keys: date, player_name, position, team,
    transaction_type, contract_value, aav.
    """
    url = f"{OTC_BASE}/transactions/{year}"
    try:
        html = await _fetch_html(url)
    except httpx.HTTPError as e:
        logger.error("OTC transactions fetch failed for %d: %s", year, e)
        return []

    soup = BeautifulSoup(html, "lxml")

    # OTC transactions page has multiple tables grouped by month/type.
    # Try to find rows with transaction data regardless of exact table structure.
    transactions = []
    for table in soup.find_all("table"):
        rows = _parse_table(soup=BeautifulSoup(str(table), "lxml"))
        for row in rows:
            # Normalize common column name variants
            entry = {
                "date": row.get("Date", row.get("date", "")),
                "player_name": row.get("Player", row.get("Name", row.get("col_1", ""))),
                "position": row.get("Pos", row.get("Position", row.get("col_2", ""))),
                "team": row.get("Team", row.get("col_3", "")),
                "transaction_type": row.get("Type", row.get("Transaction", row.get("col_4", ""))),
                "contract_value": row.get("Value", row.get("Total Value", row.get("col_5", ""))),
                "aav": row.get("AAV", row.get("Average", row.get("col_6", ""))),
            }
            if entry["player_name"]:
                transactions.append(entry)

    logger.info("Fetched %d transactions for %d from OTC", len(transactions), year)
    return transactions


async def get_contracts() -> pd.DataFrame:
    """
    Return contract data via nfl_data_py (which sources from OTC).
    Columns include: player, team, position, value, apy, guaranteed, inflated_value.
    """
    loop = asyncio.get_event_loop()
    try:
        import nfl_data_py as nfl
        df = await loop.run_in_executor(None, nfl.import_contracts)
        logger.info("Loaded %d contract records from nfl_data_py", len(df))
        return df
    except Exception as e:
        logger.error("Failed to load contracts via nfl_data_py: %s", e)
        return pd.DataFrame()


async def get_roster(team_abbr: str, season: int | None = None) -> pd.DataFrame:
    """
    Return current roster for a team using nfl_data_py (sourced from OTC).
    Falls back to OTC roster page scraping if nfl_data_py is unavailable.
    """
    from backend.integrations.nfl_data import get_rosters
    from backend.utils.seasons import get_current_season

    resolved_season = season if season is not None else get_current_season()
    rosters = await get_rosters(resolved_season)
    team_upper = team_abbr.upper()
    return rosters[rosters["team"].str.upper() == team_upper].copy()


async def get_depth_chart(team_abbr: str) -> list[dict]:
    """
    Scrape the OTC team page for depth chart signals.
    """
    slug = TEAM_SLUGS.get(team_abbr.upper())
    if not slug:
        logger.warning("Unknown team abbreviation: %s", team_abbr)
        return []

    url = f"{OTC_BASE}/position/{slug}"
    try:
        html = await _fetch_html(url)
    except httpx.HTTPError as e:
        logger.error("OTC depth chart fetch failed for %s: %s", team_abbr, e)
        return []

    soup = BeautifulSoup(html, "lxml")
    return _parse_table(soup)


async def get_all_transactions(years: list[int]) -> list[dict]:
    """Fetch transactions for multiple years concurrently."""
    results = await asyncio.gather(*[get_transactions(y) for y in years])
    combined = []
    for year, txns in zip(years, results):
        for t in txns:
            t["year"] = year
            combined.append(t)
    return combined
