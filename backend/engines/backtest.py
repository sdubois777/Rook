"""
Backtest engine — compare system projections against actual season results.

Returns structured metrics for the API endpoint and CLI script.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.integrations.nfl_data import get_seasonal_stats
from backend.models.player import Player, PlayerProfile

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

    This mirrors the actual system logic:  the valuation engine sets
    value_assessment and pay_up_flag *before* the draft, and those are
    the fields that drive bid recommendations.  Pure gap arithmetic
    can disagree when the AI ceiling is close to league price but the
    system still considers the player a buy (e.g. Nacua: pay_up_flag=True).
    """
    if pay_up_flag:
        return "strong_buy"

    if value_assessment in _BUY_ASSESSMENTS:
        return "strong_buy" if value_gap >= 5 else "buy"

    if value_assessment in _AVOID_ASSESSMENTS:
        return "strong_avoid" if value_gap <= -10 else "avoid"

    # Fallback for fair_value or missing assessment — use gap only
    if value_gap >= 5:
        return "buy"
    if value_gap <= -5:
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
        }


def _load_actual_season(season: int) -> pd.DataFrame:
    """Load actual season results via get_seasonal_stats (PBP fallback)."""
    return get_seasonal_stats(season)


async def run_backtest(session: AsyncSession, season: int = 2025) -> tuple[BacktestMetrics, pd.DataFrame]:
    """Run the backtest and return (metrics, player_df)."""

    actuals = _load_actual_season(season)

    # Build lookups
    actual_by_id: dict[str, dict] = {}
    actual_by_name: dict[str, dict] = {}
    for _, row in actuals.iterrows():
        entry = {
            "actual_ppr": float(row["fantasy_points_ppr"] or 0),
            "actual_games": int(row["games"] or 0),
        }
        actual_by_id[str(row["player_id"])] = entry
        actual_by_name[str(row["player_display_name"]).lower()] = entry

    # Load system data
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
    results = []
    matched = 0

    for player, profile in rows:
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

        league_price = float(player.market_value_league or 0)
        ai_ceiling = float(player.ai_bid_ceiling or player.recommended_bid_ceiling or 0)
        value_gap = ai_ceiling - league_price

        actual_vpd = actual_ppr / league_price if actual_ppr and league_price > 0 else None
        was_good_buy = actual_vpd is not None and actual_vpd >= FAIR_VALUE_PPR_PER_DOLLAR

        system_signal = derive_system_signal(
            value_assessment=player.value_assessment,
            pay_up_flag=bool(player.pay_up_flag),
            value_gap=value_gap,
            ai_ceiling=ai_ceiling,
            league_price=league_price,
        )

        system_correct = None
        if system_signal in ("strong_buy", "buy"):
            system_correct = was_good_buy
        elif system_signal in ("avoid", "strong_avoid"):
            system_correct = actual_vpd is not None and not was_good_buy

        injury_shortened = actual_games is not None and actual_games < 10

        results.append({
            "name": player.name,
            "position": player.position,
            "league_price": league_price,
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

    # Compute metrics
    proj_df = df[df["proj_ppr"].notna() & df["actual_ppr"].notna() & (~df["injury_shortened"])].copy()
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

    full_df = df[df["system_correct"].notna() & df["actual_ppr"].notna() & (~df["injury_shortened"])]
    metrics.total_calls = len(full_df)
    if metrics.total_calls > 0:
        metrics.signal_accuracy = round(full_df["system_correct"].sum() / metrics.total_calls * 100, 1)

    buy_df = full_df[full_df["system_signal"].isin(["strong_buy", "buy"])]
    metrics.buy_count = len(buy_df)
    if len(buy_df) > 0:
        metrics.buy_accuracy = round(buy_df["system_correct"].mean() * 100, 1)

    avoid_df = full_df[full_df["system_signal"].isin(["avoid", "strong_avoid"])]
    metrics.avoid_count = len(avoid_df)
    if len(avoid_df) > 0:
        metrics.avoid_accuracy = round(avoid_df["system_correct"].mean() * 100, 1)

    top_opps = df[(df["value_gap"] >= 8) & (~df["injury_shortened"]) & df["actual_ppr"].notna()]
    metrics.top_opportunities_count = len(top_opps)
    metrics.top_opportunities_hit = int(top_opps["was_good_buy"].sum())

    metrics.injury_excluded = int(df["injury_shortened"].sum())

    if metrics.signal_accuracy and metrics.signal_accuracy >= 65:
        metrics.grade = "STRONG"
    elif metrics.signal_accuracy and metrics.signal_accuracy >= 55:
        metrics.grade = "MODERATE"
    elif metrics.signal_accuracy and metrics.signal_accuracy >= 45:
        metrics.grade = "WEAK"
    else:
        metrics.grade = "POOR"

    return metrics, df
