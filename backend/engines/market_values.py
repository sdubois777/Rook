"""
Market value sync engine — scrapes FantasyPros auction values and updates
market_value fields on Player records.

Called by:
  - scripts/refresh_market_values.py (CLI)
  - POST /pipeline/refresh-market-values (API)
  - POST /admin/pipeline/run with agent_name="market_values" (Admin UI)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.nfl_data import normalize_player_name
from backend.models.player import Player

logger = logging.getLogger(__name__)

# Playwright needs ProactorEventLoop on Windows for subprocess support.
# Uvicorn uses SelectorEventLoop, so we run the scrape in a dedicated thread
# with its own event loop.
_pw_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="playwright")


def _scrape_in_thread(scoring_format: str, teams: int) -> tuple[list[dict], int, bool]:
    """Run the Playwright scrape in a thread with a fresh event loop."""
    from backend.integrations.fantasypros import get_best_auction_values

    loop = asyncio.new_event_loop()
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    try:
        return loop.run_until_complete(
            get_best_auction_values(format=scoring_format, teams=teams)
        )
    finally:
        loop.close()


async def sync_market_values(
    session: AsyncSession,
    scoring_format: str = "ppr",
    teams: int = 12,
    dry_run: bool = False,
) -> dict:
    """
    Scrape FantasyPros auction values and update market_value fields.

    Automatically determines which year to use via
    get_best_available_auction_year() — current season if July+,
    fallback to previous season otherwise.

    Args:
        session: Active async DB session.
        scoring_format: "ppr" | "half_ppr" | "standard". League is full PPR.
        teams: Number of teams in league.
        dry_run: If True, show matches without writing to DB.

    Returns:
        Summary dict with matched/unmatched counts, year info, and names.
    """
    logger.info("Scraping FantasyPros market values (format=%s, teams=%d)...", scoring_format, teams)
    try:
        loop = asyncio.get_running_loop()
        values, year_used, is_current = await loop.run_in_executor(
            _pw_executor, _scrape_in_thread, scoring_format, teams
        )
    except Exception as exc:
        logger.error("FantasyPros scrape failed: %s", exc)
        return {
            "matched": 0,
            "unmatched": 0,
            "unmatched_names": [],
            "updated_at": None,
            "year": None,
            "is_current_season": None,
            "error": str(exc),
        }

    if not values:
        logger.warning(
            "FantasyPros returned no data — auction values may not be published yet."
        )
        return {
            "matched": 0,
            "unmatched": 0,
            "unmatched_names": [],
            "updated_at": None,
            "year": None,
            "is_current_season": None,
            "note": "No data available from FantasyPros (not yet published for this season)",
        }

    # --- Load all players from DB ---
    players: list[Player] = (
        await session.execute(select(Player))
    ).scalars().all()

    # Build normalized name → Player lookup
    player_lookup: dict[str, Player] = {}
    for p in players:
        key = normalize_player_name(p.name)
        if key:
            player_lookup[key] = p

    # --- Match scraped data to DB players ---
    now = datetime.now(timezone.utc)
    matched = 0
    unmatched_names: list[str] = []

    for row in values:
        name = row.get("name", "")
        avg_value = row.get("avg_value")
        if avg_value is None:
            continue

        normalized = normalize_player_name(name)
        player = player_lookup.get(normalized)

        if player is None:
            unmatched_names.append(name)
            continue

        matched += 1

        if dry_run:
            logger.info(
                "DRY RUN: %s → %s (%s) auction=$%.1f",
                name, player.name, player.position, avg_value,
            )
            continue

        # Write market value fields
        player.market_value = avg_value
        player.market_value_fantasypros = avg_value
        player.market_value_confidence = _compute_confidence(row)
        player.market_value_updated_at = now
        session.add(player)

    # --- Write metadata ---
    if not dry_run and matched > 0:
        await _store_metadata(session, {
            "source": "fantasypros",
            "year": year_used,
            "is_current_season": is_current,
            "player_count": matched,
            "refreshed_at": now,
        })

    if not dry_run:
        await session.commit()

    if unmatched_names:
        logger.warning(
            "%d unmatched FantasyPros names: %s",
            len(unmatched_names),
            unmatched_names[:20],
        )

    summary = {
        "matched": matched,
        "unmatched": len(unmatched_names),
        "unmatched_names": unmatched_names[:50],
        "updated_at": now.isoformat() if not dry_run else None,
        "year": year_used,
        "is_current_season": is_current,
        "dry_run": dry_run,
    }
    logger.info(
        "Market value sync %s: %d matched, %d unmatched (year=%d, current=%s)",
        "DRY RUN" if dry_run else "complete",
        matched, len(unmatched_names), year_used, is_current,
    )
    return summary


def _compute_confidence(data: dict) -> str:
    """
    Derive confidence from the spread between min and max auction values.
    If min/max not available (DraftWizard), default to 'medium'.
    """
    avg = data.get("avg_value")
    min_val = data.get("min_value")
    max_val = data.get("max_value")

    if avg is None or min_val is None or max_val is None:
        return "medium"

    if avg <= 0:
        return "low"

    spread = max_val - min_val
    spread_pct = spread / avg if avg > 0 else 1.0

    if spread_pct <= 0.3:
        return "high"
    if spread_pct <= 0.6:
        return "medium"
    return "low"


async def _store_metadata(session: AsyncSession, data: dict) -> None:
    """Write a MarketValueMetadata record for sourcing info."""
    try:
        from backend.models.market_value_metadata import MarketValueMetadata
        record = MarketValueMetadata(
            source=data["source"],
            year=data["year"],
            is_current_season=data["is_current_season"],
            player_count=data["player_count"],
            refreshed_at=data["refreshed_at"],
        )
        session.add(record)
    except Exception:
        # Table may not exist yet (migration not run) — don't block sync
        logger.debug("MarketValueMetadata table not available, skipping metadata write")
