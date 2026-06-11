#!/usr/bin/env python
"""
Populate nfl_seasons_played for all players in the database.

Uses nflverse seasonal roster data to count how many NFL seasons
each player has appeared in (going back to 2010).

Usage:
    python scripts/populate_nfl_seasons_played.py
    python scripts/populate_nfl_seasons_played.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import nfl_data_py as nfl
import pandas as pd
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.player import Player
from backend.utils.seasons import get_current_season

# Load seasonal rosters from 2010 through the last completed season
ROSTER_YEARS = list(range(2010, get_current_season()))


def load_season_counts() -> dict[str, int]:
    """
    Load seasonal roster data from nflverse and count distinct seasons per player_id.
    Returns {gsis_player_id: number_of_seasons}.
    """
    print(f"Loading seasonal roster data ({ROSTER_YEARS[0]}-{ROSTER_YEARS[-1]})...")
    all_rosters = []
    for season in ROSTER_YEARS:
        try:
            df = nfl.import_seasonal_rosters([season])
            if not df.empty and "player_id" in df.columns:
                all_rosters.append(df[["player_id"]].assign(season=season))
                print(f"  {season}: {len(df)} players")
            else:
                print(f"  {season}: empty or missing player_id column")
        except Exception as e:
            print(f"  {season}: skipped ({e})")

    if not all_rosters:
        print("ERROR: No roster data loaded")
        return {}

    combined = pd.concat(all_rosters, ignore_index=True)

    # Count distinct seasons per player_id
    season_counts = (
        combined.groupby("player_id")["season"]
        .nunique()
        .to_dict()
    )
    print(f"\nFound {len(season_counts)} unique players across loaded seasons")
    return season_counts


async def populate(dry_run: bool = False) -> int:
    """Update nfl_seasons_played in the players table. Returns count updated."""
    lookup = load_season_counts()
    if not lookup:
        return 0

    updated = 0
    async with AsyncSessionLocal() as session:
        all_players = (await session.execute(select(Player))).scalars().all()

        for player in all_players:
            if not player.yahoo_player_id:
                continue
            # yahoo_player_id = "nfl_" + gsis_id
            gsis_id = player.yahoo_player_id.replace("nfl_", "")
            seasons_played = lookup.get(gsis_id)
            if seasons_played is not None:
                if dry_run:
                    if player.nfl_seasons_played != int(seasons_played):
                        print(f"  [DRY-RUN] {player.name}: {player.nfl_seasons_played} -> {seasons_played}")
                        updated += 1
                else:
                    player.nfl_seasons_played = int(seasons_played)
                    updated += 1

        if not dry_run and updated > 0:
            await session.commit()

    total = len(all_players)
    mode = "DRY-RUN" if dry_run else "DONE"
    print(f"\n[{mode}] Updated {updated} / {total} players with nfl_seasons_played")

    # Verification: show top veterans and rookies
    if not dry_run:
        await _verify()

    return updated


async def _verify():
    """Show sample results after populating."""
    async with AsyncSessionLocal() as session:
        # Top veterans
        result = await session.execute(
            select(Player.name, Player.nfl_seasons_played, Player.position)
            .where(Player.nfl_seasons_played.isnot(None))
            .order_by(Player.nfl_seasons_played.desc())
            .limit(10)
        )
        rows = result.all()
        print("\nTop 10 by seasons played:")
        for name, seasons, pos in rows:
            print(f"  {name:<30} {pos or '??':<4} {seasons} seasons")

        # True rookies (0 or NULL seasons)
        result = await session.execute(
            select(Player.name, Player.position)
            .where(
                (Player.nfl_seasons_played.is_(None))
                | (Player.nfl_seasons_played == 0)
            )
            .limit(10)
        )
        rookies = result.all()
        print(f"\nSample true rookies (0 or NULL seasons):")
        for name, pos in rookies:
            print(f"  {name:<30} {pos or '??'}")

        # Count summary
        result = await session.execute(
            select(Player.nfl_seasons_played)
        )
        all_vals = [r[0] for r in result.all()]
        has_val = sum(1 for v in all_vals if v is not None)
        null_val = sum(1 for v in all_vals if v is None)
        veterans = sum(1 for v in all_vals if v is not None and v >= 1)
        print(f"\nSummary: {has_val} populated, {null_val} NULL, {veterans} veterans (>=1 season)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate nfl_seasons_played for all players")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = parser.parse_args()
    asyncio.run(populate(dry_run=args.dry_run))
