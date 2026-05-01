"""
Clear valuation fields on Player rows for players that were skipped
in the last valuation pass (e.g. QBs whose profiles have empty baselines).
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile
from backend.engines.valuation import DRAFTABLE_POSITIONS, _extract_ppr


async def main() -> None:
    async with AsyncSessionLocal() as session:
        players = (
            await session.execute(
                select(Player)
                .options(selectinload(Player.profile))
                .where(Player.position.in_(DRAFTABLE_POSITIONS))
            )
        ).scalars().all()

        cleared = 0
        for p in players:
            ppr = _extract_ppr(p.profile)
            if ppr <= 0 and p.recommended_bid_ceiling is not None:
                # This player was skipped in the valuation pass but has stale data
                p.tier                    = None
                p.baseline_value          = None
                p.risk_adjusted_value     = None
                p.recommended_bid_ceiling = None
                p.let_go_threshold        = None
                p.elite_anchor_weight     = None
                p.positional_scarcity_modifier = None
                p.value_gap               = None
                p.value_gap_signal        = None
                cleared += 1
                print(f"  Cleared: {p.name} ({p.position}/{p.team_abbr})")

        await session.commit()
        print(f"\nCleared {cleared} stale valuation records.")


if __name__ == "__main__":
    asyncio.run(main())
