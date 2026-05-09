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
        })

    async with AsyncSessionLocal() as session:
        existing_result = await session.execute(select(Player.yahoo_player_id))
        existing_keys = {r[0] for r in existing_result.all()}

        new_records = [r for r in records if r["yahoo_player_id"] not in existing_keys]
        update_records = [r for r in records if r["yahoo_player_id"] in existing_keys]

        # Insert new rookies
        if new_records:
            await session.execute(
                Player.__table__.insert(),
                [
                    {
                        **r,
                        "contract_year": False,
                        "breakout_flag": False,
                        "variance_flag": True,
                    }
                    for r in new_records
                ],
            )

        # Update existing players with draft info + rookie flag
        for r in update_records:
            await session.execute(
                Player.__table__.update()
                .where(Player.yahoo_player_id == r["yahoo_player_id"])
                .values(
                    is_rookie=True,
                    draft_round=r["draft_round"],
                    draft_pick=r["draft_pick"],
                    draft_year=r["draft_year"],
                    nfl_seasons_played=0,
                    draft_capital_signal=r["draft_capital_signal"],
                    team_abbr=r["team_abbr"],
                )
            )

        await session.commit()

    print(f"  Inserted : {len(new_records)}")
    print(f"  Updated  : {len(update_records)}")
    print(f"  Skipped  : {skipped}")


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
