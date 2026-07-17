#!/usr/bin/env python
"""
Seed 2025 NFL draft picks (skill positions) into the players table.

Usage:
    python scripts/seed_draft_picks.py          # seed + verify
    python scripts/seed_draft_picks.py --verify  # verification only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import nfl_data_py as nfl
import pandas as pd
from sqlalchemy import select

from backend.database import AsyncSessionLocal
from backend.models.player import Player
from backend.utils.seasons import get_current_season

SKILL_POSITIONS = {"QB", "RB", "WR", "TE"}

# PFR team codes → standard NFL abbreviations
PFR_TEAM_MAP = {
    "GNB": "GB",
    "KAN": "KC",
    "LAR": "LA",
    "LVR": "LV",
    "NOR": "NO",
    "NWE": "NE",
    "SFO": "SF",
    "TAM": "TB",
}


def _draft_capital_signal(round_num: int) -> str:
    if round_num <= 2:
        return "high"
    if round_num <= 4:
        return "medium"
    return "low"


async def seed_draft_picks(draft_year: int | None = None):
    year = draft_year or get_current_season()
    print(f"\nSeeding {year} draft picks (skill positions) ...")

    df = nfl.import_draft_picks([year])
    skill = df[df["position"].isin(SKILL_POSITIONS)].copy()
    print(f"  Found {len(skill)} skill position picks from nfl_data_py")

    # Map PFR team codes to standard NFL abbreviations
    skill["team_nfl"] = skill["team"].map(lambda t: PFR_TEAM_MAP.get(t, t))

    records: list[dict] = []
    skipped = 0
    for _, row in skill.iterrows():
        gsis_id = row.get("gsis_id")
        name = row.get("pfr_player_name")
        if not gsis_id or pd.isna(gsis_id) or not name or pd.isna(name):
            skipped += 1
            continue

        round_num = int(row["round"])
        records.append({
            "yahoo_player_id": f"nfl_{gsis_id}",
            "name": str(name),
            "team_abbr": str(row["team_nfl"]),
            "position": str(row["position"]),
            "age": int(row["age"]) if pd.notna(row.get("age")) else None,
            "is_rookie": True,
            "draft_round": round_num,
            "draft_pick": int(row["pick"]),
            "draft_year": year,
            "nfl_seasons_played": 0,
            "draft_capital_signal": _draft_capital_signal(round_num),
            "variance_flag": True,   # rookies carry projection variance (insert-time flag)
        })

    # Route through the canonical resolver (ID-first → guarded name+pos), replacing the
    # yahoo_player_id-only dedup. A player already present (from Sleeper/nflverse) now
    # resolves and UPDATES rather than inserting a duplicate.
    #
    # VETERAN GUARD (preserved): a player can appear in draft-picks data but already have
    # nfl_seasons_played > 0 — a prior-year draftee who spent year 1 on IR (e.g. JJ
    # McCarthy 2024). Those are NOT rookies in year 2, so on a MATCH we strip is_rookie +
    # nfl_seasons_played from the incoming data (via on_update) and only refresh draft info.
    from backend.repositories.player_repo import PlayerRepository

    def _veteran_guard(existing, data):
        if existing.nfl_seasons_played and existing.nfl_seasons_played > 0:
            data.pop("is_rookie", None)
            data.pop("nfl_seasons_played", None)
            data.pop("variance_flag", None)

    inserted = updated = kept = 0
    async with AsyncSessionLocal() as session:
        repo = PlayerRepository(session)
        for r in records:
            had_history = {"kept": False}

            def _guard(existing, data, _flag=had_history):
                if existing.nfl_seasons_played and existing.nfl_seasons_played > 0:
                    _flag["kept"] = True
                _veteran_guard(existing, data)

            _, created = await repo.resolve_or_create(r, on_update=_guard)
            if created:
                inserted += 1
            elif had_history["kept"]:
                kept += 1
            else:
                updated += 1
        await session.commit()

    print(f"  Inserted : {inserted}")
    print(f"  Updated  : {updated}")
    print(f"  Kept (NFL history) : {kept}")
    print(f"  Skipped (no data)  : {skipped}")


async def verify():
    print("\n--- Verification: rookies in players table ---")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(
                Player.name,
                Player.position,
                Player.team_abbr,
                Player.draft_round,
                Player.draft_pick,
                Player.draft_capital_signal,
                Player.is_rookie,
            )
            .where(Player.is_rookie == True)
            .order_by(Player.draft_pick.asc())
            .limit(15)
        )
        rows = result.all()

        if not rows:
            print("  NO ROOKIES FOUND — seed failed!")
            return

        print(f"\n  {'Name':<25} {'Pos':<4} {'Team':<5} {'Rd':>3} {'Pick':>5} {'Capital':<8} {'Rookie'}")
        print("  " + "-" * 70)
        for r in rows:
            print(f"  {r[0]:<25} {r[1]:<4} {r[2]:<5} {r[3] or '':>3} {r[4] or '':>5} {r[5] or '':<8} {r[6]}")

        # Total count
        count_result = await session.execute(
            select(Player).where(Player.is_rookie == True)
        )
        total = len(count_result.scalars().all())
        print(f"\n  Total rookies: {total}")


async def main(verify_only: bool = False):
    if not verify_only:
        await seed_draft_picks()
    await verify()
    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed 2025 draft picks into players table")
    parser.add_argument("--verify", action="store_true", help="Verification only")
    args = parser.parse_args()
    asyncio.run(main(verify_only=args.verify))
