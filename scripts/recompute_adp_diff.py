"""One-off: recompute adp_diff + snake_flag for all valued players ($0, no LLM).

Run after sync_adp re-stores adp_fantasypros (now FantasyPros' overall RANK on
the same integer scale as adp_ai). Both sides are ranks, so:

    adp_diff = adp_fantasypros - adp_ai   (positive = FP ranks them LATER than
    us, i.e. we like them more than the market)

projected_ppr lives in the profile's clean_season_baseline, NOT on Player.

  python scripts/recompute_adp_diff.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.models.player import Player  # noqa: E402
from backend.agents.valuation_agent import (  # noqa: E402
    classify_snake_flag,
    compute_adp_diff,
)


async def recompute() -> None:
    async with AsyncSessionLocal() as db:
        players = (
            await db.execute(
                select(Player)
                .where(Player.adp_ai.isnot(None))
                .options(selectinload(Player.profile))
            )
        ).scalars().all()

        flag_counts: dict[str, int] = {}
        diff_set = 0
        for p in players:
            fp = float(p.adp_fantasypros) if p.adp_fantasypros is not None else None
            ai = float(p.adp_ai) if p.adp_ai is not None else None
            p.adp_diff = compute_adp_diff(fp, ai)
            if p.adp_diff is not None:
                diff_set += 1

            projected_ppr = None
            if p.profile and p.profile.clean_season_baseline:
                projected_ppr = p.profile.clean_season_baseline.get("ppr_points")

            flag = classify_snake_flag(
                adp_diff=float(p.adp_diff) if p.adp_diff is not None else None,
                projected_ppr=float(projected_ppr) if projected_ppr is not None else None,
                position=p.position,
            )
            p.snake_flag = flag
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

        await db.commit()

    print(f"recomputed: {len(players)} players, adp_diff set on {diff_set}")
    print("snake_flag distribution:")
    for flag, count in sorted(flag_counts.items()):
        print(f"  {flag}: {count}")


if __name__ == "__main__":
    asyncio.run(recompute())
