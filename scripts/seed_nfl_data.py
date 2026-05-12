#!/usr/bin/env python
"""
Seed script — downloads and caches 3 seasons of NFL data, seeds the players table.

Usage:
    python scripts/seed_nfl_data.py            # full run
    python scripts/seed_nfl_data.py --dry-run  # show what would be fetched
    python scripts/seed_nfl_data.py --verify   # verification only (no download)

Data is cached to data/cache/ as parquet files (gitignored).
The players table is seeded with every skill-position player from the current season.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

# Project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.player import Player
from backend.integrations import nfl_data
from backend.integrations.nfl_data import CACHE_DIR
from backend.utils.seasons import get_analysis_seasons, get_current_season

SEASONS = get_analysis_seasons(3)
SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}

# Per-season data sets fetched by download_all()
PER_SEASON_DATASETS = [
    ("weekly_data", "weekly stats"),
    ("seasonal_data", "seasonal data"),
    ("snap_counts", "snap counts"),
    ("schedules", "schedules"),
    ("target_share", "target share"),
    ("snap_pct", "snap pct"),
    ("injuries", "injuries"),
]

# One-time datasets (not per-season)
GLOBAL_DATASETS = [
    ("players", "players"),
    (f"rosters_{get_current_season()}", f"rosters {get_current_season()}"),
]


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def show_dry_run():
    """Show what would be fetched without fetching. Per COST_RULES.md Rule 7."""
    print("=" * 60)
    print("DRY RUN — showing what would be fetched")
    print(f"Seasons: {SEASONS}")
    print(f"Current season (rosters): {get_current_season()}")
    print("=" * 60)

    total = 0
    cached = 0

    for season in SEASONS:
        print(f"\n  [{season}]")
        for cache_key, label in PER_SEASON_DATASETS:
            path = CACHE_DIR / f"{cache_key}_{season}.parquet"
            hit = path.exists()
            status = "CACHED" if hit else "FETCH"
            print(f"    {label:<16} {status}")
            total += 1
            if hit:
                cached += 1

    print(f"\n  [roster / player info]")
    for cache_key, label in GLOBAL_DATASETS:
        path = CACHE_DIR / f"{cache_key}.parquet"
        hit = path.exists()
        status = "CACHED" if hit else "FETCH"
        print(f"    {label:<16} {status}")
        total += 1
        if hit:
            cached += 1

    fetches = total - cached
    print(f"\nSummary: {total} datasets, {cached} cached, {fetches} to fetch")
    print("DB seed: would insert/update skill-position players")
    print("\nNo data was downloaded. Run without --dry-run to execute.")


# ---------------------------------------------------------------------------
# Download / cache
# ---------------------------------------------------------------------------

def _try_fetch(label: str, fn, *args) -> bool:
    """Attempt a data fetch; return True on success, False on 404/error."""
    print(f"    {label:14s}...", end="", flush=True)
    try:
        fn(*args)
        print(" done")
        return True
    except Exception:
        print(" skipped (not available)")
        return False


def download_all():
    print("=" * 60)
    print("Downloading NFL data (first run may take several minutes)")
    print("=" * 60)

    for season in SEASONS:
        print(f"\n  [{season}]")

        _try_fetch("weekly stats", nfl_data.fetch_weekly_stats, season)
        _try_fetch("seasonal data", nfl_data.fetch_seasonal_data, season)
        _try_fetch("snap counts", nfl_data.fetch_snap_counts, season)
        _try_fetch("schedules", nfl_data.fetch_schedules, season)
        _try_fetch("target share", nfl_data.compute_target_share, season)
        _try_fetch("snap pct", nfl_data.compute_snap_pct, season)
        _try_fetch("injuries", nfl_data.fetch_injuries, season)

    print("\n  [roster / player info]")

    _try_fetch("players", nfl_data.fetch_players)

    current = get_current_season()
    if not _try_fetch(f"rosters {current}", nfl_data.fetch_rosters, current):
        fallback = current - 1
        _try_fetch(f"rosters {fallback}", nfl_data.fetch_rosters, fallback)

    print("\nAll data cached to data/cache/")


# ---------------------------------------------------------------------------
# DB seed
# ---------------------------------------------------------------------------

def _compute_age(birth_date_raw) -> int | None:
    if birth_date_raw is None:
        return None
    try:
        if pd.isna(birth_date_raw):
            return None
        bd = pd.to_datetime(birth_date_raw).date()
        today = date.today()
        return int(today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day)))
    except Exception:
        return None


async def seed_players():
    print("\nSeeding players table ...")

    rosters_df = nfl_data.fetch_rosters(get_current_season())

    # Deduplicate to one row per player (weekly rosters repeat per week)
    roster_skill = (
        rosters_df[rosters_df["position"].isin(SKILL_POSITIONS)]
        .sort_values("week", ascending=False)  # keep most recent week
        .drop_duplicates(subset=["player_id"])
        .copy()
    )

    # Build records list
    records: list[dict] = []
    skipped = 0
    for _, row in roster_skill.iterrows():
        gsis_id = row.get("player_id")
        name = row.get("player_name") or row.get("full_name")
        if not gsis_id or pd.isna(gsis_id) or not name or pd.isna(name):
            skipped += 1
            continue
        records.append({
            "yahoo_player_id": f"nfl_{gsis_id}",
            "gsis_id": str(gsis_id),
            "name": str(name),
            "team_abbr": str(row.get("team", "")) or None,
            "position": str(row.get("position", "")) or None,
            "age": _compute_age(row.get("birth_date")),
        })

    # Fetch existing keys in one query
    async with AsyncSessionLocal() as session:
        existing_result = await session.execute(select(Player.yahoo_player_id))
        existing_keys = {r[0] for r in existing_result.all()}

        new_records = [r for r in records if r["yahoo_player_id"] not in existing_keys]
        update_records = [r for r in records if r["yahoo_player_id"] in existing_keys]

        # Bulk insert new players
        if new_records:
            await session.execute(
                Player.__table__.insert(),
                [
                    {
                        **r,
                        "contract_year": False,
                        "breakout_flag": False,
                    }
                    for r in new_records
                ],
            )

        # Update existing (one by one but only for those that exist)
        for r in update_records:
            await session.execute(
                Player.__table__.update()
                .where(Player.yahoo_player_id == r["yahoo_player_id"])
                .values(name=r["name"], team_abbr=r["team_abbr"],
                        position=r["position"], age=r["age"],
                        gsis_id=r.get("gsis_id"))
            )

        await session.commit()

    print(f"  Inserted : {len(new_records)}")
    print(f"  Updated  : {len(update_records)}")
    print(f"  Skipped  : {skipped}")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

VERIFY_PLAYERS = [
    "Tyreek Hill",
    "CeeDee Lamb",
    "Amon-Ra St. Brown",
    "Justin Jefferson",
    "Saquon Barkley",
    "Patrick Mahomes",
]


def verify_data():
    print("\n" + "=" * 60)
    current = get_current_season()
    print(f"Verification — {current} target share for sample players")
    print("=" * 60)

    try:
        ts = nfl_data.compute_target_share(current)
        if ts.empty:
            raise ValueError("empty result")
    except Exception as e:
        print(f"Data not available for {current}: {e}")
        # Fall back to previous season for verification
        current = current - 1
        print(f"Falling back to {current} for verification")
        ts = nfl_data.compute_target_share(current)

    if ts.empty:
        print("No target share data available for verification.")
        return

    print(f"\n{'Player':<28} {'Team':<5} {'Tgts':>5} {'Tgt%':>7} {'AY%':>7} {'PPR/G':>7}")
    print("-" * 60)

    found = 0
    for name in VERIFY_PLAYERS:
        last = name.split()[-1]
        mask = ts["player_name"].str.contains(last, case=False, na=False)
        if mask.sum() == 0:
            print(f"  {name:<26} NOT FOUND")
            continue
        row = ts[mask].iloc[0]
        tgt_pct = f"{row['avg_target_share']:.1%}" if pd.notna(row.get("avg_target_share")) else "N/A"
        ay_pct  = f"{row['avg_air_yards_share']:.1%}" if pd.notna(row.get("avg_air_yards_share")) else "N/A"
        ppg     = f"{row['ppr_per_game']:.1f}" if pd.notna(row.get("ppr_per_game")) else "N/A"
        print(f"  {row['player_name']:<26} {str(row['recent_team']):<5} "
              f"{int(row['total_targets']):>5} {tgt_pct:>7} {ay_pct:>7} {ppg:>7}")
        found += 1

    print(f"\n{found}/{len(VERIFY_PLAYERS)} sample players found in {current} data.")

    # Snap count check
    print(f"\n--- Snap % sample ({current}) ---")
    try:
        snaps = nfl_data.compute_snap_pct(current)
    except Exception:
        print(f"  Snap data unavailable for {current}")
        return
    snap_check = ["Hill", "Lamb", "Jefferson", "Barkley"]
    for last in snap_check:
        mask = snaps["player"].str.contains(last, case=False, na=False)
        if mask.sum() > 0:
            row = snaps[mask].iloc[0]
            print(f"  {row['player']:<28} snap%={row['avg_snap_pct']:.1%}")


async def verify_db():
    print("\n--- DB players table ---")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Player).where(Player.position.in_(list(SKILL_POSITIONS))).limit(10)
        )
        players = result.scalars().all()
        for p in players:
            print(f"  {p.name:<30} {p.position:<4} {p.team_abbr or '':<5} age={p.age}")

        count_result = await session.execute(
            select(Player)
        )
        total = len(count_result.scalars().all())
        print(f"\n  Total players in DB: {total}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(verify_only: bool = False, dry_run: bool = False):
    if dry_run:
        show_dry_run()
        return

    if not verify_only:
        download_all()
        await seed_players()

    verify_data()
    await verify_db()

    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed NFL data into cache and database")
    parser.add_argument("--verify", action="store_true", help="Skip download, run verification only")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without fetching")
    args = parser.parse_args()

    asyncio.run(main(verify_only=args.verify, dry_run=args.dry_run))
