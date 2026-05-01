"""
Delete player_profile rows for QBs and clear their valuation fields.
QBs are not processed by player_profiles agent (SKILL_POSITIONS = WR/RB/TE only),
so any QB profiles are legacy data from earlier runs.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile


async def main() -> None:
    async with AsyncSessionLocal() as session:
        # Find QB players with profiles
        qb_players = (
            await session.execute(
                select(Player)
                .where(Player.position == "QB")
                .options(selectinload(Player.profile))
            )
        ).scalars().all()

        deleted_profiles = 0
        cleared_valuations = 0

        for p in qb_players:
            # Clear profile if exists
            if p.profile:
                await session.delete(p.profile)
                deleted_profiles += 1
                print(f"  Deleted profile: {p.name} ({p.team_abbr})")

            # Clear valuation fields if stale
            if p.recommended_bid_ceiling is not None:
                p.tier                    = None
                p.baseline_value          = None
                p.risk_adjusted_value     = None
                p.recommended_bid_ceiling = None
                p.let_go_threshold        = None
                p.elite_anchor_weight     = None
                p.positional_scarcity_modifier = None
                p.value_gap               = None
                p.value_gap_signal        = None
                cleared_valuations += 1

        await session.commit()
        print(f"\nDeleted {deleted_profiles} QB profiles, cleared {cleared_valuations} QB valuations.")


if __name__ == "__main__":
    asyncio.run(main())
