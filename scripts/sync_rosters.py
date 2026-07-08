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

from backend.utils.injury_status import to_canonical

# Strip suffixes for fuzzy name matching (III, Jr, Sr, II, IV, V)
_SUFFIX_RE = re.compile(r"\s+(III|II|IV|V|Jr\.?|Sr\.?)\s*$", re.IGNORECASE)

logger = logging.getLogger(__name__)


def is_relevant_player(player_row: dict, warehouse) -> bool:
    """Return True if a Sleeper player is relevant for fantasy drafting.

    Relevant = on the current (2026) depth chart OR appeared in a 2024/2025
    game. This filters retired / deep-practice-squad players that Sleeper
    still flags as Active (Roethlisberger, Le'Veon Bell, etc.).

    Matching:
      - Depth chart: by sleeper_id (the warehouse depth_charts frame is
        Sleeper-sourced and sleeper_id-keyed, well-populated beyond starters).
      - Recent games: by gsis_id against the seasonal-stats frame, whose
        player_id column IS the gsis id. We do NOT name-match the recent
        seasons — the 2025 frame uses abbreviated names ("C.McCaffrey").

    The depth-chart check carries the load (Sleeper gsis coverage is only
    ~16% on-team); the gsis check is a secondary catch for players with
    recent games who are off the depth chart.
    """
    # Kickers and team defenses have no skill depth-chart / usage signal to gate
    # on — the warehouse frames (depth charts, seasonal stats) are skill-only, and
    # DEF carry no gsis_id — yet every fielded team has exactly one starting K and
    # DEF, so an on-a-team K/DEF is always draft-relevant. This is the ONLY
    # K/DEF-specific branch here; all other gating stays skill-only (ingest scope).
    pos = str(player_row.get("position", "") or "").upper()
    if pos in ("K", "DEF"):
        team = player_row.get("team")
        return bool(team) and not pd.isna(team)

    sleeper_id = str(player_row.get("player_id", "") or "").strip()
    gsis_id = str(player_row.get("gsis_id", "") or "").strip()

    # Check 1 — current-season depth chart slot (Sleeper, by sleeper_id)
    dc = warehouse.depth_charts.get(2026, pd.DataFrame())
    if dc is not None and not dc.empty and sleeper_id:
        id_col = next((c for c in ("sleeper_id", "player_id") if c in dc.columns), None)
        if id_col and (dc[id_col].astype(str).str.strip() == sleeper_id).any():
            return True

    # Check 2 — game appearances in 2024/2025 (by gsis_id == stats.player_id)
    if gsis_id:
        for season in (2024, 2025):
            stats = warehouse.get_seasonal_stats(season)
            if stats is None or stats.empty or "player_id" not in stats.columns:
                continue
            if (stats["player_id"].astype(str).str.strip() == gsis_id).any():
                return True

    return False


async def sync_players_from_sleeper(
    dry_run: bool = False,
    db=None,
    warehouse=None,
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

    # Warehouse powers the recent-activity gate on new inserts. Build once
    # if the caller (pipeline) didn't pass an already-built one.
    if warehouse is None:
        from backend.integrations.nfl_data import NflDataWarehouse
        warehouse = NflDataWarehouse.build()

    updated = inserted = skipped = filtered = 0
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
        # Reverse map: stripped Sleeper name → DB player with suffix
        by_stripped_name_pos: dict[tuple[str, str], Player] = {}
        for p in all_players:
            if p.name and p.position:
                by_name_pos[(p.name.lower(), p.position.upper())] = p
                # Also index by stripped suffix (e.g., "Kenneth Walker III" → "kenneth walker")
                stripped = _SUFFIX_RE.sub("", p.name).strip().lower()
                if stripped != p.name.lower():
                    by_name_pos.setdefault((stripped, p.position.upper()), p)
                    # Reverse: Sleeper sends "Brian Thomas" → match DB "Brian Thomas Jr."
                    by_stripped_name_pos.setdefault((stripped, p.position.upper()), p)

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
            # Live injury designation for the status badge — canonical code from the
            # Sleeper injury_status already in this feed (loud-warns unknown strings).
            # Display-only: NOT a valuation input, so it never invalidates any cache.
            new_injury = to_canonical(row.get("injury_status"))

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
            # Reverse suffix match: Sleeper "Brian Thomas" → DB "Brian Thomas Jr."
            if not existing:
                existing = by_stripped_name_pos.get((full_name.lower(), position.upper()))

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
                    # Injury badge — bump the timestamp only when the code changes
                    # (display data; no cache invalidation).
                    if existing.injury_status != new_injury:
                        existing.injury_status = new_injury
                        existing.injury_status_updated_at = datetime.now(timezone.utc)

                updated += 1
            else:
                # Recent-activity gate — only seed NEW players who are on
                # the 2026 depth chart or appeared in 2024/2025 games.
                # Filters retired/practice-squad noise Sleeper marks Active
                # (Roethlisberger, Bell, Haskins). Existing players are never
                # gated here, so an active player already in the DB is never
                # dropped by a coverage gap.
                if not is_relevant_player(row, warehouse):
                    filtered += 1
                    if dry_run:
                        print(f"  [DRY-RUN] FILTERED (no depth chart, no 2024/25 games): {full_name} ({position}, {team or 'FA'})")
                    continue

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
                        injury_status=new_injury,
                        injury_status_updated_at=(
                            datetime.now(timezone.utc) if new_injury else None
                        ),
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
        "Sleeper sync: %d updated, %d inserted, %d skipped, %d filtered (irrelevant)",
        updated, inserted, skipped, filtered,
    )
    return {
        "updated": updated,
        "inserted": inserted,
        "skipped": skipped,
        "filtered": filtered,
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
          f"{result['skipped']} skipped, "
          f"{result.get('filtered', 0)} filtered (irrelevant).")
    if result.get("teams_invalidated"):
        print(f"Cache invalidated for: {result['teams_invalidated']}")


if __name__ == "__main__":
    asyncio.run(main())
