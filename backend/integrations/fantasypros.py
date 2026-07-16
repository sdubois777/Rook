"""
FantasyPros integration — auction values (Playwright) + ADP (JSON API).

AUCTION values are scraped from the DraftWizard calculator endpoint with Playwright
(the old /auction-values/ppr.php URLs were removed by FantasyPros). Confirmed still
working and genuinely per-format.

ADP is fetched as JSON from the anonymous consensus-rankings partner API. FantasyPros'
site redesign turned /nfl/adp/*-overall.php into a bot-gated virtualized table (~5 rows
to any automation), which broke the old DOM scrape; the partner API is the replacement —
no browser, no auth. See get_adp for the endpoint-choice details.

Scoring formats: 'ppr' | 'half_ppr' | 'standard'
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger(__name__)

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

# FantasyPros consensus-rankings partner API — anonymous JSON ADP (replaces the scrape).
ADP_API_URL = "https://partners.fantasypros.com/api/v1/consensus-rankings.php"
_ADP_SCORING: dict[str, str] = {"ppr": "PPR", "half_ppr": "HALF", "standard": "STD"}
_ADP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _clean_dollar(value: str) -> Optional[float]:
    try:
        return float(Decimal(value.replace("$", "").replace(",", "").strip()))
    except (InvalidOperation, ValueError):
        return None


# Roster slot query params DraftWizard's calculator accepts. The DEFAULT calculator
# URL omits the FLEX slot, so its $ reflect a rigid QB1/RB2/WR3/TE1 roster and
# under-price the flex-eligible RB/WR/TE pool. Passing an explicit roster (with
# flex=1) re-prices for a real 12-team flex league. Order = URL param order.
_ROSTER_PARAM_ORDER = ("QB", "RB", "WR", "TE", "FLEX", "DST", "K", "BN")
_ROSTER_PARAM_KEYS = {
    "QB": "qb", "RB": "rb", "WR": "wr", "TE": "te", "FLEX": "flex",
    "DST": "dst", "K": "k", "BN": "bench",
}


def _roster_query(roster: dict[str, int]) -> str:
    """Build the &qb=1&rb=2&...&flex=1&bench=6 roster-slot query fragment."""
    parts = []
    for slot in _ROSTER_PARAM_ORDER:
        if slot in roster:
            parts.append(f"&{_ROSTER_PARAM_KEYS[slot]}={int(roster[slot])}")
    return "".join(parts)


async def get_auction_values(
    scoring_format: str = "ppr",
    year: int | None = None,
    teams: int = 12,
    roster: dict[str, int] | None = None,
) -> list[dict]:
    """
    Scrape FantasyPros auction values from the DraftWizard calculator.

    Args:
        scoring_format: 'ppr' | 'half_ppr' | 'standard'
        year: Passed to URL but DraftWizard currently ignores it
              (always returns current projections). Kept for API
              compatibility and future support.
        teams: Number of teams in league (affects dollar scaling).
        roster: Optional roster-slot counts (e.g. the canonical flex-fixed shape
                {"QB":1,"RB":2,"WR":3,"TE":1,"FLEX":1,"DST":1,"K":1,"BN":6}). When
                provided, roster-slot params (incl. flex) are appended so the $
                reflect a real flex roster. When None (default), the URL is
                UNCHANGED from the pre-G5 form — the players-table market_value
                PPR path must stay byte-identical.

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
    if roster:
        url += _roster_query(roster)
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


def _num(value) -> Optional[float]:
    """Coerce an int/str/None JSON value to a clean float (or None)."""
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


async def get_adp(
    scoring_format: str = "half_ppr",
    year: int | None = None,
) -> list[dict]:
    """
    Fetch FantasyPros ADP from the anonymous consensus-rankings partner API (JSON).

    Replaces the old DOM scrape of /nfl/adp/*-overall.php, which FantasyPros' site
    redesign broke (the mcu-table is bot-gated to ~5 rows). This is a plain JSON GET —
    no Playwright, no auth/cookie — and it repairs BOTH sync_adp (players-table PPR ADP)
    and format_market (per-format adp_fantasypros).

    ENDPOINT CHOICE (verified): type=adp&position=ALL is the STANDARD 1-QB overall ADP
    (RBs/WRs at the top — Gibbs 1, Bijan 2, Chase 3 — matching the old *-overall.php
    pages). NOT type=draft/position=OP, which is the SUPERFLEX/2QB ECR (QBs at the top,
    Josh Allen #1) and would mis-rank every 1-QB league. The `scoring` param reprices per
    format (a reception-dependent player drafts later in Standard). position=ALL covers
    QB/RB/WR/TE/DST; K has no anonymous type=adp feed (returns 0) and is omitted — K ADP
    was marginal and K/DEF are valued via their own pipeline priors.

    Args:
        scoring_format: 'ppr' | 'half_ppr' | 'standard'
        year: Season to fetch; defaults to the current season (dynamic — never hardcoded).

    Returns the SAME dict shape as before (so apply_adp / build_format_market_upserts
    consume it unchanged):
      {rank, name, team, position, bye, adp, best, worst, scoring_format}

    Non-fatal on any failure (network/HTTP/parse) — returns [] so a bad pull leaves the
    last-good values in place, exactly as the old scrape did.
    """
    import httpx

    from backend.utils.seasons import get_current_season

    scoring = _ADP_SCORING.get(scoring_format)
    if not scoring:
        raise ValueError(f"Unknown scoring format '{scoring_format}'. Use: {list(_ADP_SCORING)}")

    params = {
        "sport": "NFL",
        "year": year or get_current_season(),
        "type": "adp",          # actual ADP (1-QB standard) — NOT type=draft (superflex ECR)
        "scoring": scoring,     # PPR | HALF | STD — reprices per format
        "position": "ALL",      # QB/RB/WR/TE/DST overall (position=OP is superflex ECR)
        "week": "0",
    }
    logger.info("Fetching FantasyPros ADP (%s) from %s", scoring_format, ADP_API_URL)

    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_ADP_HEADERS) as client:
            resp = await client.get(ADP_API_URL, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — a bad ADP pull must never abort a pipeline run
        logger.error("FantasyPros ADP API failed (%s): %s", scoring_format, exc)
        return []

    players: list[dict] = []
    for row in payload.get("players", []):
        rank = _num(row.get("rank_ecr"))          # overall rank (what adp_fantasypros stores)
        name = (row.get("player_name") or "").strip()
        if rank is None or not name:
            continue
        position = (row.get("player_position_id") or "").upper()
        if position == "DST":
            position = "DEF"                       # our players table uses DEF
        players.append({
            "rank":           rank,
            "name":           name,
            "team":           (row.get("player_team_id") or "").strip(),
            "position":       position,
            "bye":            _num(row.get("player_bye_week")),
            "adp":            _num(row.get("rank_ave")),   # average draft position
            "best":           _num(row.get("rank_min")),   # richer than the old scrape (was None)
            "worst":          _num(row.get("rank_max")),
            "scoring_format": scoring_format,
        })

    logger.info("Retrieved %d players from FantasyPros ADP API (%s)", len(players), scoring_format)
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
