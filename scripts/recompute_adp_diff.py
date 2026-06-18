"""One-off: recompute adp_diff + snake_flag for all valued players ($0, no LLM).

Run after sync_adp re-stores adp_fantasypros (FantasyPros' overall RANK). Both
sides are clean 1-N ranks, so:

    adp_diff = adp_fantasypros - adp_rank   (positive = FP ranks them LATER than
    us, i.e. we like them more than the market)

adp_rank (not adp_ai) is the value shown as "AI ADP" on the board, so the diff
stays consistent with both displayed columns. adp_ai has heavy ties that made
the old diff disagree with the rank shown beside it.

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
            # fp_rank - adp_rank (both clean 1-N ranks), NOT adp_ai.
            p.adp_diff = compute_adp_diff(p.adp_fantasypros, p.adp_rank)
            if p.adp_diff is not None:
                diff_set += 1

            projected_ppr = None
            if p.profile and p.profile.clean_season_baseline:
                projected_ppr = p.profile.clean_season_baseline.get("ppr_points")

            # adp_diff keeps the raw rank gap (for display); the flag is
            # neutralized past the draftable window via adp_rank.
            flag = classify_snake_flag(
                adp_diff=float(p.adp_diff) if p.adp_diff is not None else None,
                projected_ppr=float(projected_ppr) if projected_ppr is not None else None,
                position=p.position,
                adp_rank=p.adp_rank,
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
