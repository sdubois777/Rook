"""
scripts/sync_rosters.py

Quick-fix roster sync: pull current rosters from nfl_data_py (sourced from OTC)
and update all player team assignments in the database.

Usage:
    uv run python scripts/sync_rosters.py
    uv run python scripts/sync_rosters.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))


async def sync_rosters(dry_run: bool = False) -> int:
    """
    Load current rosters from nfl_data_py and update player team_abbr
    for any mismatches found in the database.
    Returns count of players updated.
    """
    from sqlalchemy import select

    from backend.database import AsyncSessionLocal
    from backend.integrations.nfl_data import fetch_rosters
    from backend.models.player import Player
    from backend.utils.seasons import get_current_season

    season = get_current_season()
    print(f"Loading rosters for {season} from nfl_data_py...")

    try:
        rosters = fetch_rosters(season)
    except Exception:
        # Current season rosters may not be published yet (nflverse lag)
        fallback = season - 1
        print(f"  {season} rosters not available, falling back to {fallback}...")
        rosters = fetch_rosters(fallback)

    # Detect column names (nfl_data_py varies by version)
    name_col = next((c for c in ("full_name", "player_name") if c in rosters.columns), None)
    team_col = next((c for c in ("team", "team_abbr") if c in rosters.columns), None)

    if not name_col or not team_col:
        print(f"ERROR: Missing expected columns. Available: {list(rosters.columns)}")
        return 0

    # Deduplicate: latest week entry per player
    if "week" in rosters.columns:
        rosters = rosters.sort_values("week", ascending=False).drop_duplicates(subset=[name_col])

    # Build name ->current team map
    roster_teams: dict[str, str] = {}
    for _, row in rosters.iterrows():
        name = str(row[name_col]).strip()
        team = str(row[team_col]).strip().upper()
        if name and team:
            roster_teams[name] = team

    print(f"Loaded {len(roster_teams)} players from nfl_data_py rosters.\n")

    updated = 0
    async with AsyncSessionLocal() as session:
        all_players = (await session.execute(select(Player))).scalars().all()

        for player in all_players:
            if not player.name:
                continue

            new_team = roster_teams.get(player.name)
            if not new_team:
                continue

            if player.team_abbr != new_team:
                old_team = player.team_abbr or "???"
                if dry_run:
                    print(f"  [DRY-RUN] {player.name}: {old_team} ->{new_team}")
                else:
                    player.team_abbr = new_team
                    player.team_updated_at = datetime.now(timezone.utc)
                    print(f"  Updated: {player.name}: {old_team} ->{new_team}")
                updated += 1

        if not dry_run and updated > 0:
            await session.commit()

    return updated


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync player team assignments from nfl_data_py rosters"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without writing to DB",
    )
    args = parser.parse_args()

    updated = await sync_rosters(dry_run=args.dry_run)
    mode = "DRY-RUN" if args.dry_run else "DONE"
    verb = "would be updated" if args.dry_run else "updated"
    print(f"\n[{mode}] {updated} player(s) {verb}.")


if __name__ == "__main__":
    asyncio.run(main())
