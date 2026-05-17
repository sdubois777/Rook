"""
Backtest: did the system correctly identify undervalued and overvalued players?

Compares pre-season system projections against actual season results.
Defaults to backtest_season = get_current_season() - 1.

This script is READ-ONLY — it never writes to any table.

Methodology:
  1. PPR ACCURACY (most reliable) — projected vs actual PPR from real game stats
  2. SIGNAL ACCURACY (approximate) — buy/avoid signals vs actual value-per-dollar
  3. TOP OPPORTUNITIES — of top value gaps, what % delivered?
  4. SPECIFIC PLAYER VALIDATION — named players the system should catch

Prices: league_auction_history for the backtest season (actual draft prices).
Falls back to market_value_league if auction history has < 50 matched players.

Injury handling: injured players ARE included in signal evaluation.
An avoid on an injured player was correct. A buy on an injured player was wrong.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.engines.backtest import (  # noqa: E402
    BacktestMetrics,
    run_backtest,
)
from backend.utils.seasons import get_current_season  # noqa: E402


def print_backtest_report(
    df: pd.DataFrame, metrics: BacktestMetrics, season: int,
) -> None:
    """Print the full backtest report with honest methodology notes."""

    print()
    print("=" * 70)
    print(f"BACKTEST ACCURACY REPORT — {season} Season")
    print("=" * 70)

    # ── Methodology banner ──
    print()
    print("  METHODOLOGY")
    print("  " + "-" * 48)
    print(f"  Actuals:          nfl_data_py game stats ({season})")
    print(f"  Price source:     {metrics.price_source}")
    print(f"  Injury handling:  Included in signal evaluation")
    print(f"  Season validated: {season} (get_current_season()-1)")
    print()
    print("  NOTE: Current signals incorporate " + str(season))
    print("  performance data in the weighted baseline.")
    print("  Signal accuracy is APPROXIMATE — not prospective.")
    print("  For rigorous validation, re-run the pipeline with")
    print(f"  analysis_seasons excluding {season}, then backtest.")

    if metrics.buy_accuracy is not None and metrics.buy_accuracy > 95:
        print()
        print("  WARNING: Buy accuracy >95% may indicate data leakage")
        print("  in the price source. Check that prices reflect pre-draft")
        print("  values, not post-season knowledge.")

    # ── Section 1: PPR accuracy (most reliable) ──
    print()
    print("  PPR ACCURACY (most reliable — no price dependency)")
    print("  " + "-" * 48)

    proj_df = df[df["proj_ppr"].notna() & df["actual_ppr"].notna()].copy()

    if len(proj_df) > 0:
        proj_df["error"] = proj_df["proj_ppr"] - proj_df["actual_ppr"]
        proj_df["abs_error"] = proj_df["error"].abs()

        mae = proj_df["abs_error"].mean()
        bias = proj_df["error"].mean()
        correlation = proj_df["proj_ppr"].corr(proj_df["actual_ppr"])
        within_20pct = (
            (proj_df["abs_error"] <= proj_df["actual_ppr"].abs() * 0.20).mean() * 100
        )

        print(f"  Players analyzed:    {len(proj_df)}")
        print(f"  Mean absolute error: {mae:.1f} PPR pts")
        print(f"  Bias:                {bias:+.1f} PPR pts")
        print(f"    (positive = system overprojects)")
        print(f"  Correlation (r):     {correlation:.3f}")
        print(f"  Within 20% actual:   {within_20pct:.0f}%")

        # Tier accuracy: did T1 outperform T2 outperform T3?
        tier_df = proj_df[proj_df["tier"].notna()].copy()
        if len(tier_df) > 0:
            tier_means = tier_df.groupby("tier")["actual_ppr"].mean().sort_index()
            if len(tier_means) >= 3:
                tiers_ordered = all(
                    tier_means.iloc[i] >= tier_means.iloc[i + 1]
                    for i in range(min(3, len(tier_means)) - 1)
                )
                tier_str = " > ".join(
                    f"T{int(t)}({v:.0f})" for t, v in tier_means.head(4).items()
                )
                print(f"  Tier ordering:       {tier_str}")
                print(f"  Tier monotonic:      {'Yes' if tiers_ordered else 'No'}")

        print()
        print("  By position:")
        for pos in ["QB", "RB", "WR", "TE"]:
            pos_df = proj_df[proj_df["position"] == pos]
            if len(pos_df) >= 3:
                print(
                    f"    {pos}: MAE={pos_df['abs_error'].mean():.1f} "
                    f"bias={pos_df['error'].mean():+.1f} "
                    f"r={pos_df['proj_ppr'].corr(pos_df['actual_ppr']):.3f} "
                    f"n={len(pos_df)}"
                )

        # Biggest misses
        print()
        print("  Biggest projection misses:")
        worst = proj_df.nlargest(5, "abs_error")
        for _, row in worst.iterrows():
            inj = " [INJ]" if row.get("injury_shortened") else ""
            print(
                f"    {row['name']:<22} Proj={row['proj_ppr']:.0f} "
                f"Actual={row['actual_ppr']:.0f} "
                f"Error={row['error']:+.0f}{inj}"
            )
    else:
        print("  No projection data available for comparison.")

    # ── Section 2: Signal accuracy (approximate) ──
    print()
    print("  SIGNAL ACCURACY (approximate — includes injured players)")
    print("  " + "-" * 48)

    signal_df = df[
        df["system_correct"].notna() & df["actual_ppr"].notna()
    ].copy()

    total_calls = len(signal_df)
    correct_calls = signal_df["system_correct"].sum() if total_calls > 0 else 0
    accuracy = correct_calls / total_calls * 100 if total_calls > 0 else 0

    print(f"  Directional calls:   {total_calls}")
    print(f"  Correct:             {correct_calls:.0f}")
    print(f"  Accuracy:            {accuracy:.1f}%")

    # Buy signal accuracy
    buy_df = signal_df[signal_df["system_signal"].isin(["strong_buy", "buy"])]
    if len(buy_df) > 0:
        buy_acc = buy_df["system_correct"].mean() * 100
        buy_injured = buy_df["injury_shortened"].sum()
        print(f"\n  BUY signals:         {len(buy_df)} players")
        print(f"    Correct:           {buy_acc:.0f}%")
        print(f"    Avg price:         ${buy_df['league_price'].mean():.0f}")
        avg_vpd = buy_df["actual_vpd"].dropna().mean()
        if pd.notna(avg_vpd):
            print(f"    Avg VPD:           {avg_vpd:.1f} PPR/$")
        if buy_injured > 0:
            print(f"    Injured (counted): {buy_injured:.0f}")

    # Avoid signal accuracy
    avoid_df = signal_df[signal_df["system_signal"].isin(["avoid", "strong_avoid"])]
    if len(avoid_df) > 0:
        avoid_acc = avoid_df["system_correct"].mean() * 100
        avoid_injured = avoid_df["injury_shortened"].sum()
        print(f"\n  AVOID signals:       {len(avoid_df)} players")
        print(f"    Correct:           {avoid_acc:.0f}%")
        print(f"    Avg price:         ${avoid_df['league_price'].mean():.0f}")
        if avoid_injured > 0:
            print(f"    Injured (counted): {avoid_injured:.0f}")

    # ── Section 3: Top opportunities ──
    print()
    print("  TOP OPPORTUNITIES")
    print("  " + "-" * 48)

    top_opps = df[
        (df["value_gap"] >= 8) & (df["league_price"] > 0)
    ].sort_values("value_gap", ascending=False).head(15)

    print(
        f"  {'Player':<22} {'Pos':4} {'Paid':6} "
        f"{'Ceil':6} {'Gap':6} {'Actual':8} "
        f"{'VPD':5} {'Result'}"
    )
    print("  " + "-" * 73)

    for _, row in top_opps.iterrows():
        if row["actual_ppr"] is None:
            result = "NO DATA"
        elif row["injury_shortened"]:
            result = f"INJ ({row['actual_games']:.0f}g)"
        elif row["was_good_buy"]:
            result = "VALUE"
        else:
            result = "MISS"

        print(
            f"  {row['name']:<22} "
            f"{row['position']:4} "
            f"${row['league_price']:4.0f} "
            f"${row['ai_ceiling']:4.0f} "
            f"{row['value_gap']:+5.1f} "
            f"{(row['actual_ppr'] or 0):7.1f} "
            f"{(row['actual_vpd'] or 0):4.1f}x "
            f"{result}"
        )

    # ── Section 4: Specific player validation ──
    print()
    print("  SPECIFIC PLAYER VALIDATION")
    print("  " + "-" * 48)

    validation_players = [
        "Rice", "Smith-Njigba", "Olave", "McCaffrey", "Robinson",
        "Pitts", "McBride", "Henry", "Chase", "Barkley",
    ]

    for search_name in validation_players:
        matches = df[df["name"].str.contains(search_name, case=False, na=False)]
        if matches.empty:
            continue
        row = matches.sort_values("league_price", ascending=False).iloc[0]

        signal_marker = {
            "strong_buy": "[BUY+]",
            "buy": "[BUY] ",
            "neutral": "[NEUT]",
            "avoid": "[AVOD]",
            "strong_avoid": "[AVD+]",
        }.get(row["system_signal"] or "", "[????]")

        if row["actual_ppr"] is None:
            outcome = "No data"
        elif row["injury_shortened"]:
            outcome = f"INJ {row['actual_games']:.0f}g — {row['actual_ppr']:.0f} PPR"
        elif row["was_good_buy"]:
            outcome = f"VALUE {row['actual_ppr']:.0f} PPR"
        else:
            outcome = f"MISS  {row['actual_ppr']:.0f} PPR"

        correct_str = ""
        if row["system_correct"] is True:
            correct_str = " (correct)"
        elif row["system_correct"] is False:
            correct_str = " (wrong)"

        price_str = f"${row['league_price']:3.0f}" if row["league_price"] > 0 else " N/A"

        print(
            f"  {signal_marker} {row['name']:<25} "
            f"Paid {price_str} | "
            f"Gap: {row['value_gap']:+5.1f} | "
            f"{outcome}{correct_str}"
        )

    # ── Section 5: Injury report ──
    injured = df[df["injury_shortened"]]
    if len(injured) > 0:
        print()
        print("  INJURED PLAYERS (included in accuracy)")
        print("  " + "-" * 48)
        injured_notable = injured[injured["league_price"] >= 10].sort_values(
            "league_price", ascending=False
        )
        for _, row in injured_notable.head(10).iterrows():
            print(
                f"    {row['name']}: {row['actual_games']:.0f}g, "
                f"paid ${row['league_price']:.0f}, "
                f"{row['actual_ppr']:.0f} PPR — "
                f"signal: {row['system_signal'] or 'none'}"
            )

    # ── Section 6: Summary ──
    print()
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)

    if accuracy >= 65:
        grade = "STRONG"
    elif accuracy >= 55:
        grade = "MODERATE"
    elif accuracy >= 45:
        grade = "WEAK"
    else:
        grade = "POOR"

    mae_val = metrics.mae
    if mae_val:
        mae_grade = (
            "Excellent (<30)"
            if mae_val < 30
            else "Good (30-50)"
            if mae_val < 50
            else "Acceptable (50-70)"
            if mae_val < 70
            else "Needs work (>70)"
        )
    else:
        mae_grade = "N/A"

    print(f"\n  Projection MAE:      {mae_val or 'N/A'} PPR — {mae_grade}")
    print(f"  Correlation:         {metrics.correlation or 'N/A'}")
    print(f"  Signal accuracy:     {accuracy:.1f}% (N={total_calls}) — {grade}")
    if len(buy_df) > 0:
        print(f"  Buy accuracy:        {buy_df['system_correct'].mean() * 100:.0f}% (N={len(buy_df)})")
    if len(avoid_df) > 0:
        print(f"  Avoid accuracy:      {avoid_df['system_correct'].mean() * 100:.0f}% (N={len(avoid_df)})")
    print(f"  Price source:        {metrics.price_source}")
    print(f"  Injured players:     {len(injured)} (included in evaluation)")

    if accuracy > 95 or (len(buy_df) > 0 and buy_df["system_correct"].mean() > 0.95):
        print()
        print("  *** SUSPICIOUSLY HIGH ACCURACY ***")
        print("  Check for data leakage in price source.")
        print("  market_value_league may contain post-season ADP.")

    print()
    print("  For prospective validation (gold standard):")
    print(f"  1. Re-run pipeline with analysis_seasons excluding {season}")
    print(f"  2. Run backtest against {season} actuals")
    print("  3. Compare signals to current results")
    print()
    print("=" * 70)


async def main() -> None:
    """Run backtest from CLI."""
    import argparse

    parser = argparse.ArgumentParser(description="Backtest system accuracy")
    parser.add_argument(
        "--season", type=int, default=None,
        help="Season to backtest against (default: current-1)",
    )
    args = parser.parse_args()

    actual_season = args.season or (get_current_season() - 1)

    print(f"\nLoading {actual_season} actual season data...")

    async with AsyncSessionLocal() as db:
        # run_backtest sets READ ONLY internally — no writes possible
        metrics, df = await run_backtest(db, actual_season)

    print(f"Matched to actual stats: {metrics.players_matched}/{metrics.players_analyzed}")

    print_backtest_report(df, metrics, actual_season)

    # Export CSV (only file write — no DB write)
    csv_path = Path(__file__).parent.parent / f"backtest_results_{actual_season}.csv"
    df.to_csv(csv_path, index=False)
    print(f"Results saved to {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
