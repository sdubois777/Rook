"""Backfill player identity onto existing league_auction_history rows.

WHY. ``LeagueSyncService._store_picks`` used to write ``player_name=""``,
``position=""`` and no ``player_id``, because Yahoo's ``draftresults`` endpoint returns
only ``player_key``. Every consumer matches on name or player_id, so an entire real
auction sat unusable — the backtest silently fell through to ``market_value_historic``
instead. The writer is fixed; this repairs rows already stored.

Recoverable because ``yahoo_player_key`` was stored: ``461.p.33963`` → the suffix is the
Yahoo player id, which is what ``players.yahoo_id`` holds.

A SCRIPT, NOT A MIGRATION — deliberately. This is a data repair whose correctness depends
on the contents of the ``players`` table, and a migration that mutates rows runs
unattended inside ``alembic upgrade head`` on every deploy. This repo has already been
bitten by putting a data fix in a migration.

AMBIGUOUS KEYS ARE SKIPPED, NEVER GUESSED. Duplicate player rows exist (18 yahoo_ids map
to more than one row); binding a real auction price to the wrong player is worse than
leaving it unresolved.

Usage:
    # dev, report only
    .venv/Scripts/python.exe scripts/backfill_auction_identity.py --dry-run

    # dev, apply
    .venv/Scripts/python.exe scripts/backfill_auction_identity.py

    # prod requires BOTH a prod DATABASE_URL and the explicit override
    PROD_DATABASE_URL=... ROOK_ALLOW_PROD_WRITES=1 \
        .venv/Scripts/python.exe scripts/backfill_auction_identity.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select, text

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_auction_identity")


async def backfill(dry_run: bool = False) -> dict:
    from backend.database import AsyncSessionLocal
    from backend.models.league_auction_history import LeagueAuctionHistory
    from backend.models.player import Player

    stats = {
        "rows": 0, "already_ok": 0, "resolved": 0,
        "unmatched": 0, "ambiguous": 0, "no_key": 0,
    }
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(LeagueAuctionHistory).where(
                LeagueAuctionHistory.player_id.is_(None)
            )
        )).scalars().all()
        stats["rows"] = len(rows)
        if not rows:
            logger.info("Nothing to backfill — every row already has a player_id.")
            return stats

        ids = {
            (r.yahoo_player_key or "").rsplit(".p.", 1)[-1]
            for r in rows
            if ".p." in (r.yahoo_player_key or "")
        }
        lookup: dict[str, tuple] = {}
        ambiguous: set[str] = set()
        if ids:
            for p in (await db.execute(
                select(Player.id, Player.yahoo_id, Player.name, Player.position)
                .where(Player.yahoo_id.in_(ids))
            )).all():
                if p.yahoo_id in lookup:
                    ambiguous.add(p.yahoo_id)
                    continue
                lookup[p.yahoo_id] = (p.id, p.name, p.position)
            for dup in ambiguous:
                lookup.pop(dup, None)

        by_season: dict[int, int] = {}
        for r in rows:
            key = r.yahoo_player_key or ""
            if ".p." not in key:
                stats["no_key"] += 1
                continue
            suffix = key.rsplit(".p.", 1)[-1]
            if suffix in ambiguous:
                stats["ambiguous"] += 1
                continue
            hit = lookup.get(suffix)
            if not hit:
                stats["unmatched"] += 1
                continue
            player_id, name, position = hit
            if not dry_run:
                r.player_id = player_id
                if not r.player_name:
                    r.player_name = name or ""
                if not r.position:
                    r.position = position or ""
                db.add(r)
            stats["resolved"] += 1
            by_season[r.season_year] = by_season.get(r.season_year, 0) + 1

        if dry_run:
            await db.rollback()
        else:
            await db.commit()

    logger.info("")
    logger.info("rows missing player_id : %d", stats["rows"])
    logger.info("  resolved             : %d", stats["resolved"])
    logger.info("  unmatched in players : %d", stats["unmatched"])
    logger.info("  ambiguous (skipped)  : %d", stats["ambiguous"])
    logger.info("  no yahoo_player_key  : %d", stats["no_key"])
    for season in sorted(by_season):
        logger.info("  season %d: %d resolved", season, by_season[season])
    logger.info("")
    logger.info("%s", "DRY RUN — nothing written." if dry_run else "Committed.")
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; write nothing.")
    args = ap.parse_args()

    if not args.dry_run:
        # Same host-based guard the pipeline uses: refuses prod unless deliberately
        # overridden. Forgetting keeps you in dev.
        from backend.db_guard import guard_writes
        guard_writes("backfill_auction_identity.py (writes league_auction_history)")

    await backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
