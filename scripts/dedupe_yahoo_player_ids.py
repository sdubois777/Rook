"""Dedupe duplicate yahoo_player_id rows on the players table.

A duplicated yahoo_player_id made the live-draft player lookup raise
MultipleResultsFound — those nominations produced NO recommendation (5/180 in a
simulated draft). The engine is now duplicate-safe, but the data should still be
fixed: for each duplicate group, KEEP the ranked/valued row and NULL the
yahoo_player_id on the others (rows are never deleted — they may carry FK
children; only the colliding platform id is cleared).

Usage:
    uv run python scripts/dedupe_yahoo_player_ids.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main(dry_run: bool) -> None:
    from sqlalchemy import func, select

    from backend.database import AsyncSessionLocal
    from backend.models.player import Player

    async with AsyncSessionLocal() as session:
        dupe_ids = (await session.execute(
            select(Player.yahoo_player_id)
            .where(Player.yahoo_player_id.isnot(None))
            .group_by(Player.yahoo_player_id)
            .having(func.count() > 1)
        )).scalars().all()

        if not dupe_ids:
            print("No duplicate yahoo_player_id groups found.")
            return

        cleared = 0
        for yid in dupe_ids:
            rows = (await session.execute(
                select(Player).where(Player.yahoo_player_id == yid)
            )).scalars().all()
            # Keep the ranked row; then the valued one; then most recently updated.
            rows = sorted(rows, key=lambda p: (
                p.adp_rank is None,
                p.baseline_value is None,
                -(float(p.baseline_value or 0)),
            ))
            keep, losers = rows[0], rows[1:]
            print(f"{yid}: KEEP {keep.name!r} (adp_rank={keep.adp_rank}) | "
                  f"CLEAR {[l.name for l in losers]}")
            for l in losers:
                l.yahoo_player_id = None
                cleared += 1

        if dry_run:
            await session.rollback()
            print(f"\nDRY RUN — would clear yahoo_player_id on {cleared} row(s) "
                  f"across {len(dupe_ids)} group(s). Nothing written.")
        else:
            await session.commit()
            print(f"\nDONE — cleared yahoo_player_id on {cleared} row(s) "
                  f"across {len(dupe_ids)} group(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args().dry_run))
