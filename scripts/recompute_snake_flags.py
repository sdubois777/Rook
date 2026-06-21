"""One-off: recompute snake_flag for all valued players deterministically.

snake_flag is derived from the post-hoc adp_diff + the VORP-derived tier — the
model can't know adp_diff at inference (it depends on the model's own adp_ai),
so this fixes the column from already-stored data with no pipeline re-run.

  python scripts/recompute_snake_flags.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.models.player import Player  # noqa: E402
from backend.agents.valuation_agent import classify_snake_flag  # noqa: E402


async def recompute():
    async with AsyncSessionLocal() as db:
        # tier (VORP) drives VALUE/SLEEPER and is a Player column — no profile load.
        players = (
            await db.execute(
                select(Player).where(Player.adp_ai.isnot(None))
            )
        ).scalars().all()

        counts = {}
        for p in players:
            # Pass adp_rank AND fp_rank so the two-sided draftable window is
            # applied here exactly as in the pipeline (this previously omitted
            # adp_rank, silently skipping the window guard). VALUE/SLEEPER uses
            # the VORP-derived tier.
            flag = classify_snake_flag(
                adp_diff=float(p.adp_diff) if p.adp_diff is not None else None,
                tier=p.tier,
                adp_rank=p.adp_rank,
                fp_rank=float(p.adp_fantasypros) if p.adp_fantasypros is not None else None,
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
