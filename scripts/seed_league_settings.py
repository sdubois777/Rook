"""
scripts/seed_league_settings.py

Seeds the league_settings table with values from docs/rules/LEAGUE_RULES.md.
Safe to run multiple times — upserts if a row already exists.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from backend.database import AsyncSessionLocal
from backend.models.league_settings import LeagueSettings


SETTINGS = {
    "platform": "Yahoo",
    "scoring_format": "PPR",
    "team_count": 12,
    "auction_budget": 200,
    "min_bid": 1,
    "skill_starter_budget": 185,
    "league_skill_dollar_pool": 2220,   # 185 × 12
    "total_roster_size": 16,
    "starting_lineup_size": 9,
    "roster_slots": {
        "QB": 1,
        "RB": 2,
        "WR": 2,
        "FLEX": 1,
        "TE": 1,
        "K": 1,
        "DEF": 1,
        "BENCH": 7,
    },
    "positional_budget_pcts": {
        "RB": 0.38,
        "WR": 0.32,
        "QB": 0.10,
        "TE": 0.10,
    },
    "replacement_level_ppr": {
        "QB": 18.0,
        "RB": 8.0,
        "WR": 7.0,
        "TE": 5.0,
    },
    "max_realistic_bid": {
        "RB": 80,
        "WR": 70,
        "QB": 50,
        "TE": 45,
        "K": 2,
        "DEF": 2,
    },
    "typical_bid_ranges": {
        "RB1": [50, 75],
        "RB2": [20, 40],
        "WR1": [40, 60],
        "WR2": [15, 30],
        "QB1": [20, 40],
        "TE1": [20, 35],
        "FLEX": [10, 25],
    },
}


async def main() -> None:
    async with AsyncSessionLocal() as session:
        existing = (await session.execute(select(LeagueSettings))).scalars().first()

        if existing:
            record = existing
            print("[seed] Updating existing league_settings row.")
        else:
            record = LeagueSettings()
            session.add(record)
            print("[seed] Creating new league_settings row.")

        for key, value in SETTINGS.items():
            setattr(record, key, value)

        await session.commit()

    print("[seed] league_settings seeded successfully.")
    print(f"       skill_starter_budget = {SETTINGS['skill_starter_budget']}")
    print(f"       league_skill_dollar_pool = {SETTINGS['league_skill_dollar_pool']}")
    print(f"       positional_budget_pcts = {SETTINGS['positional_budget_pcts']}")


if __name__ == "__main__":
    asyncio.run(main())
