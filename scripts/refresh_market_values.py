"""
scripts/refresh_market_values.py

Scrapes FantasyPros auction values and updates market_value fields on Player records.
Automatically determines which year to pull (current season if July+, previous otherwise).
Optionally re-runs the valuation pass so bid ceilings reflect the new market data.

Usage:
    uv run python scripts/refresh_market_values.py --dry-run
    uv run python scripts/refresh_market_values.py
    uv run python scripts/refresh_market_values.py --revalue

Market values should be refreshed within 72 hours of the actual draft.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh market values from FantasyPros auction data"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show matches without writing to DB",
    )
    parser.add_argument(
        "--revalue",
        action="store_true",
        help="Re-run valuation pass after syncing market values",
    )
    args = parser.parse_args()

    from backend.utils.seasons import get_fantasypros_auction_year

    year, is_current = get_fantasypros_auction_year()

    print("\n=== Market Value Refresh ===")
    print("  Checking FantasyPros data availability...")
    if is_current:
        print(f"  Month is July+ — using current season data ({year})")
    else:
        print(f"  Month is before July — current season data not yet available")
        print(f"  Using {year} season data (fallback)")

    if args.dry_run:
        print("  Mode: DRY RUN (no DB writes)\n")

    from backend.database import AsyncSessionLocal
    from backend.engines.market_values import sync_market_values

    async with AsyncSessionLocal() as session:
        result = await sync_market_values(
            session,
            scoring_format="ppr",
            dry_run=args.dry_run,
        )

    year_used = result.get("year")
    is_current_result = result.get("is_current_season")

    if result.get("error"):
        print(f"\n  Error: {result['error']}")
        print()
        return

    if result.get("note"):
        print(f"\n  {result['note']}")
        print()
        return

    print(f"\n  Scraped and matched {result['matched']} players "
          f"from FantasyPros ({year_used} PPR auction)")
    print(f"  Unmatched: {result['unmatched']} players")

    if result.get("unmatched_names"):
        print(f"\n  Unmatched names (first 20):")
        for name in result["unmatched_names"][:20]:
            print(f"    - {name}")

    if result.get("updated_at"):
        print(f"\n  Updated at: {result['updated_at']}")

    # Summary banner
    season_label = "current" if is_current_result else "previous"
    print(f"\n  Market value source: FantasyPros {year_used} PPR ({season_label} season)")
    if not is_current_result:
        next_year = (year_used or 0) + 1
        print(f"  Note: refresh again in July for {next_year} season values")

    # Optionally re-run valuations
    if args.revalue and not args.dry_run and result["matched"] > 0:
        print("\n=== Re-running Valuation Pass ===")
        from backend.engines.valuation import run_valuation_pass

        val_result = await run_valuation_pass()
        print(
            f"  Updated  : {val_result['updated']} players\n"
            f"  Skipped  : {val_result['skipped']} players\n"
            f"  Year     : {val_result['analysis_year']}"
        )

    print()


if __name__ == "__main__":
    asyncio.run(main())
