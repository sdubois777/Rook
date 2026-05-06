"""
scripts/compute_valuations.py

Runs the draft-bible valuation pass (Stage 9 — pure Python, no AI calls).

Usage:
    uv run python scripts/compute_valuations.py --dry-run
    uv run python scripts/compute_valuations.py
    uv run python scripts/compute_valuations.py --check-only

Reads ppr_points from player_profiles.clean_season_baseline,
applies PAR-based auction-value mapping, writes bid ceilings and tiers
back to the players table.

Calibration (per docs/rules/LEAGUE_RULES.md):
    LEAGUE_SKILL_DOLLAR_POOL = $185 × 12 = $2,220
    RB=38%, WR=32%, QB=10%, TE=10% of skill dollar pool
    Any ceiling above $80 is logged as a warning.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def print_dry_run() -> None:
    from backend.engines.valuation import (
        LEAGUE_SKILL_BUDGET,
        LEAGUE_SKILL_DOLLAR_POOL,
        LEAGUE_TEAMS,
        POSITION_BUDGET_SHARE,
        MAX_REALISTIC_BID,
        get_draftable_pool_sizes,
    )
    pool_sizes = get_draftable_pool_sizes(LEAGUE_TEAMS)
    print("\n=== Valuation Pass — Dry Run ===")
    print(f"  Skill budget : ${LEAGUE_SKILL_BUDGET}/team × {LEAGUE_TEAMS} teams = ${LEAGUE_SKILL_DOLLAR_POOL}")
    print(f"  No API calls — pure Python computation")
    print(f"\n  Draftable pool sizes (dynamic from league settings):")
    for pos, size in sorted(pool_sizes.items()):
        print(f"    {pos}: {size} players")
    print(f"    Total: {sum(pool_sizes.values())} skill position players")
    print(f"\n  Positional budget allocation:")
    for pos, pct in POSITION_BUDGET_SHARE.items():
        budget = LEAGUE_SKILL_DOLLAR_POOL * pct
        print(f"    {pos}: {pct:.0%} -> ${budget:.0f}")
    print(f"\n  Bid ceiling sanity caps: {MAX_REALISTIC_BID}")
    print(f"\n  To run: uv run python scripts/compute_valuations.py")
    print()


async def check_only() -> None:
    """Print current bid ceilings without re-computing."""
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models.player import Player

    async with AsyncSessionLocal() as session:
        players = (
            await session.execute(
                select(Player)
                .where(Player.recommended_bid_ceiling.is_not(None))
                .order_by(Player.recommended_bid_ceiling.desc())
                .limit(20)
            )
        ).scalars().all()

    print("\n=== Current Top 20 Bid Ceilings ===")
    for p in players:
        ceiling = float(p.recommended_bid_ceiling)
        sv      = float(p.baseline_value or 0)
        flag    = " *** >$80 ***" if ceiling > 80 else ""
        print(
            f"  {p.name:<30} {p.position:<3} T{p.tier or '?'}  "
            f"sv=${sv:>5.0f}  ceiling=${ceiling:>5.2f}{flag}"
        )
    print()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the draft-bible valuation pass")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print calibration parameters only — no DB writes",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Print current bid ceilings from DB — no computation",
    )
    args = parser.parse_args()

    if args.dry_run:
        print_dry_run()
        return

    if args.check_only:
        await check_only()
        return

    # Real run
    from backend.engines.valuation import run_valuation_pass

    print("\n=== Valuation Pass ===")
    result = await run_valuation_pass()
    print(
        f"  Updated  : {result['updated']} players\n"
        f"  Skipped  : {result['skipped']} players (no profile)\n"
        f"  Year     : {result['analysis_year']}"
    )

    # Show pool sizes and replacement levels
    if "pool_sizes" in result:
        print(f"\n  Pool sizes:")
        for pos, size in sorted(result["pool_sizes"].items()):
            repl = result["replacement_levels"].get(pos, 0)
            print(f"    {pos}: {size} players, replacement = {repl:.1f} PPR")

    # Show sanity check warnings
    if result.get("warnings"):
        print(f"\n  SANITY CHECK WARNINGS:")
        for w in result["warnings"]:
            print(f"    WARNING: {w}")
    else:
        print(f"\n  Sanity checks: all passed.")

    # Surface any >$80 warnings
    from sqlalchemy import select
    from backend.database import AsyncSessionLocal
    from backend.models.player import Player

    async with AsyncSessionLocal() as session:
        over_cap = (
            await session.execute(
                select(Player)
                .where(Player.recommended_bid_ceiling > 80)
                .order_by(Player.recommended_bid_ceiling.desc())
            )
        ).scalars().all()

    if over_cap:
        print(f"\n  WARNING: {len(over_cap)} player(s) exceed $80 sanity cap -- verify calibration:")
        for p in over_cap:
            print(
                f"     {p.name} ({p.position}, T{p.tier}): "
                f"sv=${float(p.baseline_value or 0):.0f}, "
                f"ceiling=${float(p.recommended_bid_ceiling):.2f}"
            )
    else:
        print("  All ceilings within $80 sanity cap.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
