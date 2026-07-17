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

import pandas as pd

# Ensure project root is on path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.repositories.player_repo import PlayerRepository
from backend.utils.injury_status import to_canonical

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
    from sqlalchemy import delete

    from backend.integrations.sleeper import fetch_sleeper_players

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
        # Route EVERY row through the canonical resolver (ID-first → guarded name+pos),
        # replacing the old in-memory 4-key matching. Identity/dedup now lives in ONE
        # place (PlayerRepository.resolve_or_create), so a player already inserted by
        # another source (e.g. an nflverse-seeded row) resolves + updates instead of a
        # duplicate. Current-state semantics are preserved via the hooks below.
        repo = PlayerRepository(session)

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

            data = {
                "name": full_name, "position": position, "team_abbr": team,
                "sleeper_id": sleeper_id, "sportradar_id": sportradar, "gsis_id": gsis,
                "age": age, "nfl_seasons_played": years_exp,
                "depth_chart_order": new_depth, "injury_status": new_injury,
            }

            # DRY-RUN: resolve (find-only) + report; never mutate/insert.
            if dry_run:
                existing = await repo.resolve_player(
                    sleeper_id=sleeper_id, sportradar_id=sportradar, gsis_id=gsis,
                    name=full_name, position=position, team=team,
                )
                if existing is not None:
                    if existing.team_abbr != team:
                        print(f"  [DRY-RUN] {full_name}: {existing.team_abbr or '???'} -> {team}")
                    updated += 1
                elif is_relevant_player(row, warehouse):
                    print(f"  [DRY-RUN] NEW: {full_name} ({position}, {team or 'FA'})")
                    inserted += 1
                else:
                    print(f"  [DRY-RUN] FILTERED (no depth chart, no 2024/25 games): {full_name} ({position}, {team or 'FA'})")
                    filtered += 1
                continue

            # On MATCH, diff the pre-update row for cache invalidation + apply the
            # CURRENT-STATE injury field (which may CLEAR to None on recovery — the field
            # union never blanks, so it's applied directly here with its timestamp).
            def _on_update(existing, d, _team=team, _depth=new_depth, _injury=new_injury):
                if existing.team_abbr != _team:
                    if existing.team_abbr:
                        changed_teams.add(existing.team_abbr)   # old team
                    if _team:
                        changed_teams.add(_team)                # new team
                    d["team_updated_at"] = datetime.now(timezone.utc)
                if _depth is not None and existing.depth_chart_order != _depth and _team:
                    changed_teams.add(_team)                     # depth shift
                if existing.injury_status != _injury:
                    existing.injury_status = _injury            # may be None (recovery)
                    existing.injury_status_updated_at = datetime.now(timezone.utc)
                d.pop("injury_status", None)                    # handled above (allow blank)

            # Recent-activity gate — only INSERT genuinely-new players on the 2026 depth
            # chart or with 2024/25 games (existing players always update, never gated).
            player, created = await repo.resolve_or_create(
                data,
                allow_create=lambda _row=row: is_relevant_player(_row, warehouse),
                on_update=_on_update,
            )
            if player is None:
                filtered += 1
            elif created:
                if team:
                    changed_teams.add(team)                     # new player → invalidate team
                # New players carry the injury timestamp only when injured (parity w/ old insert).
                player.injury_status_updated_at = (
                    datetime.now(timezone.utc) if new_injury else None
                )
                inserted += 1
            else:
                updated += 1

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
