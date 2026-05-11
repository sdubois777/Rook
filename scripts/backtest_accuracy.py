"""
Backtest: did the system correctly identify undervalued and overvalued players?

Compares pre-season system projections against actual season results.
Uses 2024 actual data (2025 season hasn't happened yet).

Metrics:
  1. PROJECTION ACCURACY — MAE, bias, correlation of projected_ppr vs actual
  2. VALUE GAP ACCURACY — when system said buy/avoid, was it right?
  3. TOP OPPORTUNITIES — of top value gaps, what % delivered?
  4. AVOID ACCURACY — of players flagged avoid, what % underperformed?
  5. SPECIFIC PLAYER VALIDATION — named players the system should catch
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pandas as pd

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))

import nfl_data_py as nfl  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend.database import AsyncSessionLocal  # noqa: E402
from backend.models.player import Player, PlayerProfile  # noqa: E402

# $1 of auction spend should return ~3.8 PPR points for "fair value"
# ($200 budget, ~760 total PPR from starters)
FAIR_VALUE_PPR_PER_DOLLAR = 3.8


def load_actual_season(season: int) -> pd.DataFrame:
    """Load actual season results from nfl_data_py weekly data, aggregated."""
    cols = [
        "player_id", "player_display_name", "position",
        "recent_team", "fantasy_points_ppr", "season_type",
    ]
    weekly = nfl.import_weekly_data([season], cols)
    weekly = weekly[
        (weekly["season_type"] == "REG")
        & (weekly["position"].isin(["QB", "RB", "WR", "TE"]))
    ]
    seasonal = (
        weekly.groupby(["player_id", "player_display_name", "position", "recent_team"])
        .agg(
            games=("fantasy_points_ppr", "count"),
            fantasy_points_ppr=("fantasy_points_ppr", "sum"),
        )
        .reset_index()
    )
    # Keep best team entry per player (most games)
    seasonal = seasonal.sort_values("games", ascending=False).drop_duplicates("player_id")
    return seasonal


async def run_backtest(actual_season: int = 2024) -> pd.DataFrame:
    """Run the full backtest and return player-level results DataFrame."""

    # ── Load actual results ──────────────────────────────
    print(f"Loading {actual_season} actual season data...")
    try:
        actuals = load_actual_season(actual_season)
    except Exception as e:
        print(f"Could not load {actual_season}: {e}")
        if actual_season != 2024:
            print("Falling back to 2024...")
            actual_season = 2024
            actuals = load_actual_season(2024)
        else:
            raise

    print(f"Using {actual_season} actual results: {len(actuals)} skill players")

    # Build lookup: gsis_id -> actual stats
    actual_by_id: dict[str, dict] = {}
    actual_by_name: dict[str, dict] = {}
    for _, row in actuals.iterrows():
        entry = {
            "actual_ppr": float(row["fantasy_points_ppr"] or 0),
            "actual_games": int(row["games"] or 0),
            "actual_name": row["player_display_name"],
        }
        actual_by_id[str(row["player_id"])] = entry
        # Also index by lowercase display name for fallback
        actual_by_name[str(row["player_display_name"]).lower()] = entry

    # ── Load system data from DB ─────────────────────────
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Player, PlayerProfile)
            .join(
                PlayerProfile,
                Player.id == PlayerProfile.player_id,
                isouter=True,
            )
            .where(Player.market_value_league.isnot(None))
            .where(Player.market_value_league > 1)
            .where(Player.position.in_(["QB", "RB", "WR", "TE"]))
        )
        rows = result.fetchall()

    print(f"System players with league prices: {len(rows)}")

    # ── Match and compute ────────────────────────────────
    results = []
    matched = 0

    for player, profile in rows:
        # Match by gsis_id (strip "nfl_" prefix from yahoo_player_id)
        actual = None
        if player.yahoo_player_id:
            gsis = player.yahoo_player_id.replace("nfl_", "")
            actual = actual_by_id.get(gsis)

        # Fallback: match by name
        if actual is None:
            actual = actual_by_name.get(player.name.lower())

        if actual:
            matched += 1
            actual_ppr = actual["actual_ppr"]
            actual_games = actual["actual_games"]
        else:
            actual_ppr = None
            actual_games = None

        # System projection
        proj_ppr = None
        if profile and profile.clean_season_baseline:
            proj_ppr = (
                profile.clean_season_baseline.get("projected_ppr_season")
                or profile.clean_season_baseline.get("ppr_points")
            )
            if proj_ppr is not None:
                proj_ppr = float(proj_ppr)

        league_price = float(player.market_value_league or 0)
        ai_ceiling = float(
            player.ai_bid_ceiling
            or player.recommended_bid_ceiling
            or 0
        )

        value_gap = ai_ceiling - league_price

        # Actual value per dollar
        actual_vpd = (
            actual_ppr / league_price
            if actual_ppr and league_price > 0
            else None
        )

        was_good_buy = (
            actual_vpd is not None and actual_vpd >= FAIR_VALUE_PPR_PER_DOLLAR
        )

        # System signal
        if value_gap >= 8:
            system_signal = "strong_buy"
        elif value_gap >= 3:
            system_signal = "buy"
        elif value_gap >= -3:
            system_signal = "neutral"
        elif value_gap >= -8:
            system_signal = "avoid"
        else:
            system_signal = "strong_avoid"

        # Was system right?
        system_correct = None
        if system_signal in ("strong_buy", "buy"):
            system_correct = was_good_buy
        elif system_signal in ("avoid", "strong_avoid"):
            system_correct = actual_vpd is not None and not was_good_buy

        injury_shortened = actual_games is not None and actual_games < 10

        results.append({
            "name": player.name,
            "position": player.position,
            "tier": player.tier,
            "league_price": league_price,
            "ai_ceiling": ai_ceiling,
            "value_gap": round(value_gap, 1),
            "system_signal": system_signal,
            "value_assessment": player.value_assessment,
            "proj_ppr": proj_ppr,
            "actual_ppr": actual_ppr,
            "actual_games": actual_games,
            "actual_vpd": round(actual_vpd, 2) if actual_vpd else None,
            "was_good_buy": was_good_buy,
            "system_correct": system_correct,
            "injury_shortened": injury_shortened,
        })

    print(f"Matched to actual stats: {matched}/{len(rows)}")

    df = pd.DataFrame(results)
    print_backtest_report(df, actual_season)

    # Export CSV
    csv_path = Path(__file__).parent.parent / f"backtest_results_{actual_season}.csv"
    df.to_csv(csv_path, index=False)
    print(f"\nResults saved to {csv_path}")

    return df


def print_backtest_report(df: pd.DataFrame, season: int) -> dict:
    """Print the full backtest report and return summary metrics."""

    print("\n" + "=" * 70)
    print(f"BACKTEST ACCURACY REPORT - {season} Season")
    print(f"Using pre-{season + 1} system data vs actual {season} results")
    print("=" * 70)

    summary: dict = {"season": season}

    # -- SECTION 1: PROJECTION ACCURACY ---------------------
    print("\n  PROJECTION ACCURACY")
    print("-" * 50)

    proj_df = df[
        df["proj_ppr"].notna()
        & df["actual_ppr"].notna()
        & (~df["injury_shortened"])
    ].copy()

    if len(proj_df) > 0:
        proj_df["error"] = proj_df["proj_ppr"] - proj_df["actual_ppr"]
        proj_df["abs_error"] = proj_df["error"].abs()

        mae = proj_df["abs_error"].mean()
        bias = proj_df["error"].mean()
        correlation = proj_df["proj_ppr"].corr(proj_df["actual_ppr"])
        within_20pct = (
            (proj_df["abs_error"] <= proj_df["actual_ppr"].abs() * 0.20).mean() * 100
        )

        summary["mae"] = round(mae, 1)
        summary["bias"] = round(bias, 1)
        summary["correlation"] = round(correlation, 3)

        print(f"Players analyzed:      {len(proj_df)}")
        print(f"Mean absolute error:   {mae:.1f} PPR pts")
        print(f"Bias:                  {bias:+.1f} PPR pts")
        print(f"  (positive = system overprojects)")
        print(f"  (negative = system underprojects)")
        print(f"Correlation (r):       {correlation:.3f}")
        print(f"Within 20% of actual:  {within_20pct:.0f}%")

        print("\nBy position:")
        for pos in ["QB", "RB", "WR", "TE"]:
            pos_df = proj_df[proj_df["position"] == pos]
            if len(pos_df) >= 3:
                print(
                    f"  {pos}: MAE={pos_df['abs_error'].mean():.1f} "
                    f"bias={pos_df['error'].mean():+.1f} "
                    f"r={pos_df['proj_ppr'].corr(pos_df['actual_ppr']):.3f} "
                    f"n={len(pos_df)}"
                )

        # Biggest misses
        print("\nBiggest projection misses (healthy players):")
        worst = proj_df.nlargest(5, "abs_error")
        for _, row in worst.iterrows():
            print(
                f"  {row['name']:<22} Proj={row['proj_ppr']:.0f} "
                f"Actual={row['actual_ppr']:.0f} "
                f"Error={row['error']:+.0f}"
            )
    else:
        print("No projection data available for comparison.")
        summary["mae"] = None

    # -- SECTION 2: VALUE GAP ACCURACY ----------------------
    print("\n  VALUE GAP ACCURACY (Buy/Avoid Signals)")
    print("-" * 50)

    signal_df = df[
        df["system_correct"].notna() & df["actual_ppr"].notna()
    ].copy()
    full_season_df = signal_df[~signal_df["injury_shortened"]]

    total_calls = len(full_season_df)
    correct_calls = full_season_df["system_correct"].sum()
    accuracy = correct_calls / total_calls * 100 if total_calls > 0 else 0

    summary["signal_accuracy"] = round(accuracy, 1)
    summary["total_calls"] = total_calls

    print(f"Directional calls made: {total_calls}")
    print(f"Correct:                {correct_calls:.0f}")
    print(f"Accuracy:               {accuracy:.1f}%")

    # Buy signal accuracy
    buy_df = full_season_df[
        full_season_df["system_signal"].isin(["strong_buy", "buy"])
    ]
    if len(buy_df) > 0:
        buy_acc = buy_df["system_correct"].mean() * 100
        summary["buy_accuracy"] = round(buy_acc, 1)
        print(f"\nBUY signals:            {len(buy_df)} players")
        print(f"  Were good buys:       {buy_acc:.0f}%")
        print(f"  Avg league price:     ${buy_df['league_price'].mean():.0f}")
        avg_vpd = buy_df["actual_vpd"].dropna().mean()
        print(f"  Avg actual VPD:       {avg_vpd:.1f} PPR/$")

    # Avoid signal accuracy
    avoid_df = full_season_df[
        full_season_df["system_signal"].isin(["avoid", "strong_avoid"])
    ]
    if len(avoid_df) > 0:
        avoid_acc = avoid_df["system_correct"].mean() * 100
        summary["avoid_accuracy"] = round(avoid_acc, 1)
        print(f"\nAVOID signals:          {len(avoid_df)} players")
        print(f"  Were bad buys:        {avoid_acc:.0f}%")
        print(f"  Avg league price:     ${avoid_df['league_price'].mean():.0f}")

    # -- SECTION 3: TOP OPPORTUNITIES -----------------------
    print("\n  TOP OPPORTUNITIES VALIDATION")
    print("-" * 50)
    print("Players system flagged as biggest value gaps")
    print("(ai_ceiling >> league_price)\n")

    top_opps = df[df["value_gap"] >= 8].sort_values("value_gap", ascending=False).head(15)

    print(
        f"{'Player':<22} {'Pos':4} {'Paid':6} "
        f"{'Ceil':6} {'Gap':6} {'Actual':8} "
        f"{'VPD':5} {'Result'}"
    )
    print("-" * 75)

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
            f"{row['name']:<22} "
            f"{row['position']:4} "
            f"${row['league_price']:4.0f} "
            f"${row['ai_ceiling']:4.0f} "
            f"{row['value_gap']:+5.1f} "
            f"{(row['actual_ppr'] or 0):7.1f} "
            f"{(row['actual_vpd'] or 0):4.1f}x "
            f"{result}"
        )

    # -- SECTION 4: SPECIFIC VALIDATION PLAYERS -------------
    print("\n  SPECIFIC PLAYER VALIDATION")
    print("-" * 50)

    validation_players = [
        "Rice",
        "Smith-Njigba",
        "Olave",
        "McCaffrey",
        "Robinson",  # Bijan
        "Pitts",
        "McBride",
        "Henry",
        "Chase",
        "Barkley",
    ]

    for search_name in validation_players:
        matches = df[df["name"].str.contains(search_name, case=False, na=False)]
        if matches.empty:
            continue
        # Pick the one with highest league price
        row = matches.sort_values("league_price", ascending=False).iloc[0]

        signal_marker = {
            "strong_buy": "[BUY+]",
            "buy": "[BUY] ",
            "neutral": "[NEUT]",
            "avoid": "[AVOD]",
            "strong_avoid": "[AVD+]",
        }.get(row["system_signal"], "[????]")

        if row["injury_shortened"]:
            outcome = f"INJ {row['actual_games']:.0f}g"
        elif row["actual_ppr"] is None:
            outcome = "No data"
        elif row["was_good_buy"]:
            outcome = f"VALUE {row['actual_ppr']:.0f} PPR"
        else:
            outcome = f"MISS  {row['actual_ppr']:.0f} PPR"

        correct_str = ""
        if row["system_correct"] is True:
            correct_str = " (correct)"
        elif row["system_correct"] is False:
            correct_str = " (wrong)"

        print(
            f"  {signal_marker} {row['name']:<25} "
            f"Paid ${row['league_price']:3.0f} | "
            f"Gap: {row['value_gap']:+5.1f} | "
            f"{outcome}{correct_str}"
        )

    # -- SECTION 5: SUMMARY --------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    if accuracy >= 65:
        grade = "STRONG - System provides genuine edge"
    elif accuracy >= 55:
        grade = "MODERATE - Better than coin flip"
    elif accuracy >= 45:
        grade = "WEAK - Needs calibration work"
    else:
        grade = "POOR - Significant issues"

    summary["grade"] = grade

    print(f"\nOverall signal accuracy: {accuracy:.1f}%")
    print(f"Grade: {grade}")

    if summary.get("mae"):
        mae_grade = (
            "Excellent (< 30)"
            if summary["mae"] < 30
            else "Good (30-50)"
            if summary["mae"] < 50
            else "Acceptable (50-70)"
            if summary["mae"] < 70
            else "Needs work (> 70)"
        )
        print(f"Projection MAE: {summary['mae']:.1f} PPR pts - {mae_grade}")

    # Injury caveat
    injured = df[df["injury_shortened"]]
    print(f"\nInjury-shortened seasons: {len(injured)} players")
    print("(Excluded from accuracy metrics - can't predict injuries)")

    if len(injured) > 0:
        print("\nKey injured players (excluded from accuracy):")
        injured_notable = injured[injured["league_price"] >= 10].sort_values(
            "league_price", ascending=False
        )
        for _, row in injured_notable.head(10).iterrows():
            print(
                f"  {row['name']}: {row['actual_games']:.0f} games, "
                f"paid ${row['league_price']:.0f} - "
                f"system said {row['system_signal']}"
            )

    print("\n" + "=" * 70)

    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backtest system accuracy")
    parser.add_argument("--season", type=int, default=2024, help="Season to backtest against")
    args = parser.parse_args()

    asyncio.run(run_backtest(args.season))
