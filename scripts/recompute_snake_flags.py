"""One-off: recompute snake_flag for all valued players deterministically.

snake_flag is derived from the post-hoc adp_diff + projected production — the
model can't know adp_diff at inference (it depends on the model's own adp_ai),
so this fixes the column from already-stored data with no pipeline re-run.

  python scripts/recompute_snake_flags.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.models.player import Player  # noqa: E402
from backend.agents.valuation_agent import classify_snake_flag  # noqa: E402


async def recompute():
    async with AsyncSessionLocal() as db:
        players = (
            await db.execute(
                select(Player)
                .where(Player.adp_ai.isnot(None))
                .options(selectinload(Player.profile))
            )
        ).scalars().all()

        counts = {}
        for p in players:
            # projected_ppr lives in the profile's clean_season_baseline,
            # not on the Player row.
            projected_ppr = None
            if p.profile and p.profile.clean_season_baseline:
                projected_ppr = p.profile.clean_season_baseline.get("ppr_points")
            flag = classify_snake_flag(
                adp_diff=float(p.adp_diff) if p.adp_diff is not None else None,
                projected_ppr=float(projected_ppr) if projected_ppr is not None else None,
                position=p.position,
            )
            p.snake_flag = flag
            counts[flag] = counts.get(flag, 0) + 1
        await db.commit()

    print("snake_flag distribution:")
    for flag, count in sorted(counts.items()):
        print(f"  {flag}: {count}")
    print(f"  total: {sum(counts.values())}")


if __name__ == "__main__":
    asyncio.run(recompute())
