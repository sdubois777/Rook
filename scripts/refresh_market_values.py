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
        "--no-revalue",
        action="store_true",
        help="Skip the valuation + signal recompute after syncing (leaves the board's "
             "value gap computed against the price this run replaced)",
    )
    parser.add_argument(
        "--revalue",
        action="store_true",
        help="Accepted for compatibility and ignored — revaluing is now the default. "
             "Use --no-revalue to skip it.",
    )
    args = parser.parse_args()

    from backend.utils.seasons import get_fantasypros_auction_year

    year, _ = get_fantasypros_auction_year()

    print("\n=== Market Value Refresh ===")
    print(f"  Fetching {year} season auction values from FantasyPros...")

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

    # Names that DID match but matched several player rows. Reported separately from
    # unmatched because these were skipped deliberately rather than not found, and each
    # one is a duplicate-row problem in the players table worth fixing at the source.
    if result.get("ambiguous"):
        print(f"\n  Skipped as ambiguous ({result['ambiguous']} matched several "
              f"player rows; priced none rather than guessing):")
        for name in result.get("ambiguous_names", [])[:20]:
            print(f"    - {name}")

    if result.get("updated_at"):
        print(f"\n  Updated at: {result['updated_at']}")

    # Summary banner
    print(f"\n  Market value source: FantasyPros {year_used} PPR")

    # RECOMPUTE BY DEFAULT what the new price invalidates — and ONLY that.
    #
    # The sync writes only the price columns, but the board's value gap, its buy/sell
    # signal and the "top opportunities" ranking are DERIVED from the price and stored,
    # not computed at read time. Leaving them means the board shows the new price beside
    # a gap computed from the price it replaced, so the two numbers on one row no longer
    # subtract.
    #
    # reconcile_value_signals is exactly the right pass: it rewrites value_gap,
    # value_gap_signal, value_assessment, pay_up_flag, nomination_target_flag and
    # signal_conviction from the (unchanged) bid ceiling against the (new) market, and
    # touches no ceiling.
    #
    # It deliberately does NOT run run_valuation_pass. That pass rewrites
    # recommended_bid_ceiling from projections, which a market move did not change, and
    # doing it here would be wrong twice over: outside the pipeline it has no
    # prior_production argument, so the displaced-direction guard behaves differently
    # from the pipeline's own call and the board reprices inconsistently; and it would
    # leave recommended_bid_ceiling recomputed while ai_bid_ceiling — owned by the
    # valuation agent and then railed by the positional budget enforcement — stayed as
    # it was. A price refresh must not silently rescale the board.
    if not args.no_revalue and not args.dry_run and result["matched"] > 0:
        print("\n=== Recomputing market-relative signals against the new prices ===")
        from backend.engines.valuation import reconcile_value_signals

        rec = await reconcile_value_signals()
        print(
            f"  Signals  : {rec['updated']} player(s) reconciled; "
            f"pay_up={rec['flag_counts']['pay_up']}, "
            f"nomination_target={rec['flag_counts']['nomination_target']}"
        )
    elif args.no_revalue and result["matched"] > 0:
        print("\n  NOTE: --no-revalue was passed. The board now holds new prices beside "
              "\n  value gaps computed against the OLD ones. Run the pre-draft pipeline, "
              "\n  or this command without --no-revalue, before using the board.")

    print()


if __name__ == "__main__":
    asyncio.run(main())
