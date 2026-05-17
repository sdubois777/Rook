"""
scripts/sync_rosters.py

Sync player roster data from Sleeper API.
Updates team assignments, IDs (sportradar_id, sleeper_id, gsis_id),
and inserts new players not yet in the database.

After sync, invalidates agent cache (player_profiles, roster_changes)
only for teams with meaningful changes (team moves, depth chart changes,
new players). ID-only updates do not invalidate.

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


async def sync_players_from_sleeper(
    dry_run: bool = False,
    db=None,
) -> dict:
    """
    Sync players table from Sleeper API.

    Updates: team_abbr, depth_chart_order, sleeper_id, sportradar_id,
             gsis_id, age, years_exp
    Inserts: new skill-position players not yet in DB

    Tracks which teams had meaningful changes (team moves, depth chart
    changes, new players) and invalidates only those teams' agent caches.

    Args:
        dry_run: Show changes without writing to DB.
        db: Optional AsyncSession for testing. Creates its own if None.

    Returns dict with counts: updated, inserted, skipped,
    teams_invalidated, cache_cleared.
    """
    from sqlalchemy import select, delete

    from backend.integrations.sleeper import fetch_sleeper_players
    from backend.models.player import Player

    players_df = fetch_sleeper_players()
    print(f"Loaded {len(players_df)} active skill players from Sleeper.\n")

    updated = inserted = skipped = 0
    changed_teams: set[str] = set()

    if db is None:
        from backend.database import AsyncSessionLocal
        session_ctx = AsyncSessionLocal()
    else:
        session_ctx = None

    session = db if db is not None else await session_ctx.__aenter__()
    try:
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
            new_depth = (
                int(row["depth_chart_order"])
                if pd.notna(row.get("depth_chart_order"))
                else None
            )

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
                # Track meaningful changes — team move
                if existing.team_abbr != team:
                    if not dry_run:
                        if existing.team_abbr:
                            changed_teams.add(existing.team_abbr)  # old team
                        if team:
                            changed_teams.add(team)  # new team
                        existing.team_abbr = team
                        existing.team_updated_at = datetime.now(timezone.utc)
                    else:
                        print(f"  [DRY-RUN] {full_name}: {existing.team_abbr or '???'} -> {team}")

                # Track meaningful changes — depth chart shift
                if (
                    new_depth is not None
                    and existing.depth_chart_order != new_depth
                ):
                    if not dry_run:
                        if team:
                            changed_teams.add(team)
                        existing.depth_chart_order = new_depth

                # Always update IDs — prevents stale cross-player collisions
                # ID-only updates don't invalidate cache
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
                # Insert new player — invalidate their team
                if not dry_run:
                    if team:
                        changed_teams.add(team)
                    new_player = Player(
                        name=full_name,
                        position=position,
                        team_abbr=team,
                        sleeper_id=sleeper_id,
                        sportradar_id=sportradar,
                        gsis_id=gsis,
                        age=age,
                        depth_chart_order=new_depth,
                    )
                    session.add(new_player)
                else:
                    print(f"  [DRY-RUN] NEW: {full_name} ({position}, {team or 'FA'})")
                inserted += 1

        if not dry_run:
            await session.commit()

            # Invalidate profile cache for changed teams only
            if changed_teams:
                from backend.models.agent_cache import AgentCache

                await session.execute(
                    delete(AgentCache).where(
                        AgentCache.agent_name.in_([
                            "player_profiles",
                            "roster_changes",
                        ]),
                        AgentCache.entity_id.in_(changed_teams),
                    )
                )
                await session.commit()

                logger.info(
                    "Roster sync: invalidated profile cache "
                    "for %d teams with changes: %s",
                    len(changed_teams),
                    sorted(changed_teams),
                )
            else:
                logger.info(
                    "Roster sync: no team changes detected "
                    "— profile cache untouched"
                )
    finally:
        if session_ctx is not None:
            await session_ctx.__aexit__(None, None, None)

    logger.info(
        "Sleeper sync: %d updated, %d inserted, %d skipped",
        updated, inserted, skipped,
    )
    return {
        "updated": updated,
        "inserted": inserted,
        "skipped": skipped,
        "teams_invalidated": sorted(changed_teams),
        "cache_cleared": len(changed_teams) > 0,
    }


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
    if result.get("teams_invalidated"):
        print(f"Cache invalidated for: {result['teams_invalidated']}")


if __name__ == "__main__":
    asyncio.run(main())
