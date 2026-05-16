"""
scripts/sync_rosters.py

Sync player roster data from Sleeper API.
Updates team assignments, IDs (sportradar_id, sleeper_id, gsis_id),
and inserts new players not yet in the database.

Matching priority (most to least reliable):
  1. sportradar_id  (100% coverage in Sleeper)
  2. gsis_id        (29% coverage)
  3. full_name + position (fallback)

Usage:
    uv run python scripts/sync_rosters.py
    uv run python scripts/sync_rosters.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import re

import pandas as pd

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

# Strip suffixes for fuzzy name matching (III, Jr, Sr, II, IV, V)
_SUFFIX_RE = re.compile(r"\s+(III|II|IV|V|Jr\.?|Sr\.?)\s*$", re.IGNORECASE)

logger = logging.getLogger(__name__)


async def sync_players_from_sleeper(dry_run: bool = False) -> dict:
    """
    Sync players table from Sleeper API.

    Updates: team_abbr, sleeper_id, sportradar_id, gsis_id, age, years_exp
    Inserts: new skill-position players not yet in DB

    Returns dict with counts: updated, inserted, skipped.
    """
    from sqlalchemy import select, func

    from backend.database import AsyncSessionLocal
    from backend.integrations.sleeper import fetch_sleeper_players
    from backend.models.player import Player

    players_df = fetch_sleeper_players()
    print(f"Loaded {len(players_df)} active skill players from Sleeper.\n")

    updated = inserted = skipped = 0

    async with AsyncSessionLocal() as session:
        # Pre-load all existing players for batch matching
        all_players = (await session.execute(select(Player))).scalars().all()

        # Build lookup maps for efficient matching
        by_sportradar = {p.sportradar_id: p for p in all_players if p.sportradar_id}
        by_gsis = {p.gsis_id: p for p in all_players if p.gsis_id}
        by_name_pos = {}
        for p in all_players:
            if p.name and p.position:
                by_name_pos[(p.name.lower(), p.position.upper())] = p
                # Also index by stripped suffix (e.g., "Kenneth Walker III" → "kenneth walker")
                stripped = _SUFFIX_RE.sub("", p.name).strip().lower()
                if stripped != p.name.lower():
                    by_name_pos.setdefault((stripped, p.position.upper()), p)

        for _, row in players_df.iterrows():
            sleeper_id = str(row["player_id"])
            full_name = row.get("full_name", "")
            position = row.get("position", "")
            team = row.get("team") if pd.notna(row.get("team")) else None
            sportradar = (
                str(row["sportradar_id"])
                if pd.notna(row.get("sportradar_id"))
                else None
            )
            gsis = (
                str(row["gsis_id"])
                if pd.notna(row.get("gsis_id"))
                else None
            )
            age = int(row["age"]) if pd.notna(row.get("age")) else None
            years_exp = int(row["years_exp"]) if pd.notna(row.get("years_exp")) else None

            if not full_name or not position:
                skipped += 1
                continue

            # Find existing player: sportradar_id > gsis_id > name+position
            # Position must match to prevent cross-position collisions
            # (e.g., WR Kenneth Walker vs RB Kenneth Walker)
            existing = None
            if sportradar:
                cand = by_sportradar.get(sportradar)
                if cand and cand.position == position:
                    existing = cand
            if not existing and gsis:
                cand = by_gsis.get(gsis)
                if cand and cand.position == position:
                    existing = cand
            if not existing:
                existing = by_name_pos.get((full_name.lower(), position.upper()))

            if existing:
                old_team = existing.team_abbr
                changed = False

                # Update team if changed (includes setting to None for FAs)
                if existing.team_abbr != team:
                    if dry_run:
                        print(f"  [DRY-RUN] {full_name}: {old_team or '???'} -> {team}")
                    else:
                        existing.team_abbr = team
                        existing.team_updated_at = datetime.now(timezone.utc)
                    changed = True

                # Always update IDs — prevents stale cross-player collisions
                if not dry_run:
                    existing.sleeper_id = sleeper_id
                    if sportradar:
                        existing.sportradar_id = sportradar
                    if gsis:
                        existing.gsis_id = gsis
                    if age is not None:
                        existing.age = age
                    if years_exp is not None:
                        existing.nfl_seasons_played = years_exp

                updated += 1
            else:
                # Insert new player
                if dry_run:
                    print(f"  [DRY-RUN] NEW: {full_name} ({position}, {team or 'FA'})")
                else:
                    new_player = Player(
                        name=full_name,
                        position=position,
                        team_abbr=team,
                        sleeper_id=sleeper_id,
                        sportradar_id=sportradar,
                        gsis_id=gsis,
                        age=age,
                    )
                    session.add(new_player)
                inserted += 1

        if not dry_run:
            await session.commit()

    logger.info(
        "Sleeper sync: %d updated, %d inserted, %d skipped",
        updated, inserted, skipped,
    )
    return {"updated": updated, "inserted": inserted, "skipped": skipped}


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync player roster data from Sleeper API"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without writing to DB",
    )
    args = parser.parse_args()

    result = await sync_players_from_sleeper(dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "DONE"
    print(f"\n[{mode}] {result['updated']} updated, "
          f"{result['inserted']} inserted, "
          f"{result['skipped']} skipped.")


if __name__ == "__main__":
    asyncio.run(main())
