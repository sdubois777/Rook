"""
Backtest engine — compare system projections against actual season results.

Returns structured metrics for the API endpoint and CLI script.

METHODOLOGY NOTES:
  - Actuals: real NFL game stats from nfl_data_py (never system projections)
  - Prices: league_auction_history for the backtest season (actual draft prices)
  - Fallback: market_value_league if auction history coverage is insufficient
  - Injury handling: injured players ARE evaluated (an avoid on an injured player
    was correct; a buy on an injured player was wrong)
  - Signal limitation: current signals incorporate backtest-season data in the
    weighted baseline. Accuracy is approximate, not prospective.
  - Read-only: this module never writes to any table
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.nfl_data import get_seasonal_stats
from backend.models.league_auction_history import LeagueAuctionHistory
from backend.models.player import Player, PlayerProfile
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)

FAIR_VALUE_PPR_PER_DOLLAR = 3.8

_BUY_ASSESSMENTS = {"elite_value", "good_value"}
_AVOID_ASSESSMENTS = {"avoid", "slight_overpay"}


def derive_system_signal(
    value_assessment: str | None,
    pay_up_flag: bool,
    value_gap: float,
    ai_ceiling: float | None,
    league_price: float,
) -> str:
    """Derive backtest signal from value_assessment + pay_up_flag (primary)
    with value_gap as secondary confirmation.

    Key rules:
    1. pay_up_flag always wins → strong_buy
    2. Cheap players (price <= $8) never avoid — downside is negligible
    3. Small negative gaps (-8 to 0) are auction noise → neutral
    4. Only flag avoid for meaningful gaps (< -8) with confirming assessment
    """
    # Pay up flag always wins
    if pay_up_flag:
        return "strong_buy"

    # RULE 1: Cheap players ($1-8) should never be avoid regardless of
    # slight_overpay tag.  Downside is negligible — treat as neutral.
    if league_price <= 8:
        if value_assessment in _BUY_ASSESSMENTS:
            return "strong_buy"
        return "neutral"

    # RULE 2: Small negative gaps (-8 to 0) are within auction noise —
    # not actionable avoids.  Downgrade to neutral.
    if -8 <= value_gap <= 0:
        if value_assessment in _BUY_ASSESSMENTS:
            return "buy"
        return "neutral"

    # RULE 3: Buy signals (positive gap or good assessment)
    if value_assessment in _BUY_ASSESSMENTS:
        return "strong_buy" if value_gap >= 5 else "buy"

    # RULE 4: Avoid signals only for meaningful gaps AND confirming assessment
    if value_assessment in _AVOID_ASSESSMENTS:
        if value_gap <= -15:
            return "strong_avoid"
        if value_gap <= -8:
            return "avoid"
        return "neutral"

    # Default: neutral with gap fallback
    if value_gap >= 5:
        return "buy"
    if value_gap <= -8:
        return "avoid"
    return "neutral"


@dataclass
class BacktestMetrics:
    season: int
    players_analyzed: int = 0
    players_matched: int = 0
    mae: float | None = None
    bias: float | None = None
    correlation: float | None = None
    within_20pct: float | None = None
    signal_accuracy: float | None = None
    total_calls: int = 0
    buy_accuracy: float | None = None
    buy_count: int = 0
    avoid_accuracy: float | None = None
    avoid_count: int = 0
    top_opportunities_count: int = 0
    top_opportunities_hit: int = 0
    injury_excluded: int = 0
    grade: str = ""
    position_mae: dict = field(default_factory=dict)
    price_source: str = ""
    price_coverage: int = 0

    def to_dict(self) -> dict:
        return {
            "season": self.season,
            "players_analyzed": self.players_analyzed,
            "players_matched": self.players_matched,
            "projection": {
                "mae": self.mae,
                "bias": self.bias,
                "correlation": self.correlation,
                "within_20pct": self.within_20pct,
                "by_position": self.position_mae,
            },
            "signals": {
                "accuracy": self.signal_accuracy,
                "total_calls": self.total_calls,
                "buy_accuracy": self.buy_accuracy,
                "buy_count": self.buy_count,
                "avoid_accuracy": self.avoid_accuracy,
                "avoid_count": self.avoid_count,
            },
            "top_opportunities": {
                "flagged": self.top_opportunities_count,
                "delivered": self.top_opportunities_hit,
            },
            "injury_excluded": self.injury_excluded,
            "grade": self.grade,
            "price_source": self.price_source,
            "price_coverage": self.price_coverage,
        }


def _load_actual_season(season: int) -> pd.DataFrame:
    """Load actual season results via get_seasonal_stats (PBP fallback)."""
    return get_seasonal_stats(season)


async def _load_historical_prices(
    session: AsyncSession, season: int,
) -> tuple[dict[str, float], str]:
    """Load historical auction prices from league_auction_history.

    Returns (name_to_price dict, source_label).
    Falls back to market_value_league if auction history has < 50 players.
    """
    # Try league_auction_history first
    result = await session.execute(
        select(
            LeagueAuctionHistory.player_name,
            func.avg(LeagueAuctionHistory.price).label("avg_price"),
        )
        .where(
            LeagueAuctionHistory.season_year == season,
            LeagueAuctionHistory.price > 0,
            LeagueAuctionHistory.player_name.isnot(None),
            LeagueAuctionHistory.player_name != "",
        )
        .group_by(LeagueAuctionHistory.player_name)
    )
    rows = result.fetchall()
    history_prices = {
        row.player_name: float(row.avg_price) for row in rows
    }

    if len(history_prices) >= 50:
        return history_prices, f"league_auction_history ({season}, N={len(history_prices)})"

    # Also try matching via player_id (auction rows may have player_id but no name)
    result2 = await session.execute(
        select(
            Player.name,
            func.avg(LeagueAuctionHistory.price).label("avg_price"),
        )
        .join(Player, LeagueAuctionHistory.player_id == Player.id)
        .where(
            LeagueAuctionHistory.season_year == season,
            LeagueAuctionHistory.price > 0,
        )
        .group_by(Player.name)
    )
    rows2 = result2.fetchall()
    for row in rows2:
        if row.name not in history_prices:
            history_prices[row.name] = float(row.avg_price)

    if len(history_prices) >= 50:
        return history_prices, f"league_auction_history ({season}, N={len(history_prices)})"

    # Fallback: use market_value_league from players table
    # This may contain current ADP data rather than historical prices —
    # flag this clearly in the output
    return {}, "market_value_league (fallback — auction history insufficient)"


async def run_backtest(
    session: AsyncSession, season: int | None = None,
) -> tuple[BacktestMetrics, pd.DataFrame]:
    """Run the backtest and return (metrics, player_df).

    Read-only: never writes to any table.
    """
    if season is None:
        season = get_current_season() - 1

    # Enforce read-only transaction
    await session.execute(text("SET TRANSACTION READ ONLY"))

    actuals = _load_actual_season(season)

    # Build lookups
    actual_by_id: dict[str, dict] = {}
    actual_by_name: dict[str, dict] = {}
    name_col = "player_display_name" if "player_display_name" in actuals.columns else "player_name"
    for _, row in actuals.iterrows():
        entry = {
            "actual_ppr": float(row["fantasy_points_ppr"] or 0),
            "actual_games": int(row["games"] or 0),
        }
        actual_by_id[str(row["player_id"])] = entry
        actual_by_name[str(row[name_col]).lower()] = entry

    # Load historical prices
    historical_prices, price_source = await _load_historical_prices(session, season)
    using_historical = len(historical_prices) >= 50

    # Load system data — use market_value_league OR historical prices to determine
    # which players to include
    if using_historical:
        # Select ALL skill players with projections (not gated by market_value_league)
        result = await session.execute(
            select(Player, PlayerProfile)
            .join(PlayerProfile, Player.id == PlayerProfile.player_id, isouter=True)
            .where(Player.position.in_(["QB", "RB", "WR", "TE"]))
        )
    else:
        # Fallback: gate on market_value_league like before
        result = await session.execute(
            select(Player, PlayerProfile)
            .join(PlayerProfile, Player.id == PlayerProfile.player_id, isouter=True)
            .where(Player.market_value_league.isnot(None))
            .where(Player.market_value_league > 1)
            .where(Player.position.in_(["QB", "RB", "WR", "TE"]))
        )
    rows = result.fetchall()

    metrics = BacktestMetrics(season=season)
    metrics.players_analyzed = len(rows)
    metrics.price_source = price_source
    metrics.price_coverage = len(historical_prices) if using_historical else 0
    results = []
    matched = 0

    for player, profile in rows:
        # Get price: historical auction price > market_value_league
        if using_historical:
            price = historical_prices.get(player.name)
            if price is None:
                # Player not in auction history — skip from price-based evaluation
                # but still include for PPR accuracy
                price = 0.0
        else:
            price = float(player.market_value_league or 0)

        if price <= 0:
            # No meaningful price — include for PPR accuracy but not signal accuracy
            pass

        # Match to actuals
        actual = None
        if player.yahoo_player_id:
            gsis = player.yahoo_player_id.replace("nfl_", "")
            actual = actual_by_id.get(gsis)
        if actual is None:
            actual = actual_by_name.get(player.name.lower())

        if actual:
            matched += 1
            actual_ppr = actual["actual_ppr"]
            actual_games = actual["actual_games"]
        else:
            actual_ppr = None
            actual_games = None

        proj_ppr = None
        if profile and profile.clean_season_baseline:
            proj_ppr = (
                profile.clean_season_baseline.get("projected_ppr_season")
                or profile.clean_season_baseline.get("ppr_points")
            )
            if proj_ppr is not None:
                proj_ppr = float(proj_ppr)

        ai_ceiling = float(player.ai_bid_ceiling or player.recommended_bid_ceiling or 0)
        value_gap = ai_ceiling - price if price > 0 else 0.0

        actual_vpd = actual_ppr / price if actual_ppr and price > 0 else None
        was_good_buy = actual_vpd is not None and actual_vpd >= FAIR_VALUE_PPR_PER_DOLLAR

        # Only compute signals for players with a meaningful price
        if price > 0:
            system_signal = derive_system_signal(
                value_assessment=player.value_assessment,
                pay_up_flag=bool(player.pay_up_flag),
                value_gap=value_gap,
                ai_ceiling=ai_ceiling,
                league_price=price,
            )

            # Was system right? Injured players count — a buy signal on an injured
            # player was wrong (they didn't deliver value)
            system_correct = None
            if system_signal in ("strong_buy", "buy"):
                system_correct = was_good_buy
            elif system_signal in ("avoid", "strong_avoid"):
                system_correct = actual_vpd is not None and not was_good_buy
        else:
            system_signal = None
            system_correct = None

        injury_shortened = actual_games is not None and actual_games < 10

        results.append({
            "name": player.name,
            "position": player.position,
            "tier": player.tier,
            "league_price": price,
            "ai_ceiling": ai_ceiling,
            "value_gap": round(value_gap, 1),
            "system_signal": system_signal,
            "value_assessment": player.value_assessment,
            "pay_up_flag": bool(player.pay_up_flag),
            "proj_ppr": proj_ppr,
            "actual_ppr": actual_ppr,
            "actual_games": actual_games,
            "actual_vpd": round(actual_vpd, 2) if actual_vpd else None,
            "was_good_buy": was_good_buy,
            "system_correct": system_correct,
            "injury_shortened": injury_shortened,
        })

    metrics.players_matched = matched
    df = pd.DataFrame(results)

    # ── PPR accuracy (most reliable — no price dependency) ──
    proj_df = df[df["proj_ppr"].notna() & df["actual_ppr"].notna()].copy()
    if len(proj_df) > 0:
        proj_df["error"] = proj_df["proj_ppr"] - proj_df["actual_ppr"]
        proj_df["abs_error"] = proj_df["error"].abs()
        metrics.mae = round(proj_df["abs_error"].mean(), 1)
        metrics.bias = round(proj_df["error"].mean(), 1)
        metrics.correlation = round(proj_df["proj_ppr"].corr(proj_df["actual_ppr"]), 3)
        metrics.within_20pct = round(
            (proj_df["abs_error"] <= proj_df["actual_ppr"].abs() * 0.20).mean() * 100, 1
        )
        for pos in ["QB", "RB", "WR", "TE"]:
            pos_df = proj_df[proj_df["position"] == pos]
            if len(pos_df) >= 3:
                metrics.position_mae[pos] = round(pos_df["abs_error"].mean(), 1)

    # ── Signal accuracy (includes injured players) ──
    signal_df = df[
        df["system_correct"].notna() & df["actual_ppr"].notna()
    ]
    metrics.total_calls = len(signal_df)
    if metrics.total_calls > 0:
        metrics.signal_accuracy = round(
            signal_df["system_correct"].sum() / metrics.total_calls * 100, 1
        )

    buy_df = signal_df[signal_df["system_signal"].isin(["strong_buy", "buy"])]
    metrics.buy_count = len(buy_df)
    if len(buy_df) > 0:
        metrics.buy_accuracy = round(buy_df["system_correct"].mean() * 100, 1)

    avoid_df = signal_df[signal_df["system_signal"].isin(["avoid", "strong_avoid"])]
    metrics.avoid_count = len(avoid_df)
    if len(avoid_df) > 0:
        metrics.avoid_accuracy = round(avoid_df["system_correct"].mean() * 100, 1)

    top_opps = df[(df["value_gap"] >= 8) & df["actual_ppr"].notna()]
    metrics.top_opportunities_count = len(top_opps)
    metrics.top_opportunities_hit = int(top_opps["was_good_buy"].sum())

    metrics.injury_excluded = int(df["injury_shortened"].sum())

    if metrics.signal_accuracy is not None and metrics.signal_accuracy >= 65:
        metrics.grade = "STRONG"
    elif metrics.signal_accuracy is not None and metrics.signal_accuracy >= 55:
        metrics.grade = "MODERATE"
    elif metrics.signal_accuracy is not None and metrics.signal_accuracy >= 45:
        metrics.grade = "WEAK"
    else:
        metrics.grade = "POOR"

    return metrics, df
