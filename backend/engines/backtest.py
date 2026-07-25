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
from backend.models.market_value_historic import MarketValueHistoric
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
    projected_ppr: float | None = None,
) -> str:
    """Derive backtest signal from value_assessment + pay_up_flag (primary)
    with value_gap as secondary confirmation.

    Key rules:
    1. pay_up_flag always wins → strong_buy
    2. Cheap players (price <= $12) never avoid — downside is negligible
    3. Low-projection players (< 80 PPR) never avoid — depth noise
    4. Small negative gaps (-8 to 0) are auction noise → neutral
    5. Only flag avoid for meaningful gaps (< -8) with confirming assessment
    """
    # Pay up flag always wins
    if pay_up_flag:
        return "strong_buy"

    # RULE 1: Cheap players ($1-12) should never be avoid regardless of
    # slight_overpay tag.  Downside is negligible — treat as neutral.
    if league_price <= 12:
        if value_assessment in _BUY_ASSESSMENTS:
            return "strong_buy"
        return "neutral"

    # RULE 1b: Low-projection players (< 80 PPR) are depth — avoid is noise.
    if projected_ppr is not None and projected_ppr < 80:
        if value_assessment in _BUY_ASSESSMENTS:
            return "buy" if value_gap >= 5 else "neutral"
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


def apply_price_relative_outcome(df: pd.DataFrame) -> pd.DataFrame:
    """Rewrite `was_good_buy` as "did he beat the slot you paid for", within position.

    WHY THE FLAT BAR HAD TO GO. The previous rule was
    ``was_good_buy = actual_ppr / price >= 3.8``. Value-per-dollar is mechanically
    larger at low prices, so a flat bar across a $1-$65 auction is close to
    unfalsifiable at the bottom. Measured on the real 2025 board:

        price bucket   n    % clearing 3.8   median vpd
        $1-3          64          98%             80.0
        $4-10         32         100%             23.4
        $11-25        24          88%             11.3
        $26-45        24          83%              5.8
        $46+          13          54%              4.8

    So the metric mostly reported which price band a call landed in. It scored our
    top opportunities 38/38 — but those have a median price of $3, where 98% of ALL
    players "hit". Buys skewed cheap and passed automatically (94.9%); avoids landed
    on players who cleared the bar anyway (39.3%, worse than a coin flip).

    THE REPLACEMENT. Within each position, fit what the market's price actually bought
    — actual points against log(price) — and take the sign of the residual:

        was_good_buy := actual_ppr > (a + b * ln(price))

    A player is a good buy when he out-scored what his own price predicted. Least
    squares puts the residuals symmetrically about the fit, so the base rate is ~50% at
    EVERY price level by construction and any accuracy above it is real discrimination
    rather than a reflection of what we happened to call cheap.

    A RANK-VS-RANK VERSION WAS TRIED FIRST AND IS WRONG. `actual_rank <= price_rank`
    looks price-neutral but is not: the cheapest player has price_rank = N, so his
    condition is satisfied automatically, while the most expensive must finish outright
    first. Measured base rate ran 59% at $1-3 down to 15% at $46+ — the same artifact
    inverted, not removed.

    log(price) because auction prices are heavily right-skewed ($1 to $65 here, with 38
    players at exactly $1); a linear fit would let the handful of expensive players set
    the slope for the whole board.

    Fitted WITHIN POSITION on purpose: QBs out-score RBs in raw PPR, so one pooled fit
    would mark most QBs good buys and most RBs bad ones.

    Only drafted players with a real result are scored; everyone else keeps
    ``was_good_buy = False`` and is excluded from signal scoring by the caller.
    The legacy flat-bar verdict is preserved as `was_good_buy_flat_vpd`.
    """
    import numpy as np

    if df.empty or "was_good_buy" not in df.columns:
        return df

    df = df.copy()
    df["was_good_buy_flat_vpd"] = df["was_good_buy"]
    df["price_implied_ppr"] = np.nan
    # system_correct is tri-state (True / False / None for "not scoreable"). When every
    # row happened to be scoreable pandas infers a bool column, and writing None back
    # into it raises LossySetitemError — so pin it to object before the writes below.
    df["system_correct"] = df["system_correct"].astype(object)

    scoreable = df["actual_ppr"].notna() & (df["league_price"] > 0)
    for pos in df.loc[scoreable, "position"].dropna().unique():
        sel = scoreable & (df["position"] == pos)
        # Need enough spread to fit a line; below that a residual is meaningless.
        if sel.sum() < 4 or df.loc[sel, "league_price"].nunique() < 2:
            df.loc[sel, "was_good_buy"] = False
            continue
        x = np.log(df.loc[sel, "league_price"].astype(float).to_numpy())
        y = df.loc[sel, "actual_ppr"].astype(float).to_numpy()
        b, a = np.polyfit(x, y, 1)
        predicted = a + b * x
        df.loc[sel, "price_implied_ppr"] = np.round(predicted, 1)
        df.loc[sel, "was_good_buy"] = y > predicted

    df.loc[~scoreable, "was_good_buy"] = False

    # system_correct is derived from was_good_buy, so it must be recomputed. An avoid
    # is correct when the player did NOT beat his slot; both directions require a real
    # result, so unscoreable rows go back to None and drop out of the metrics.
    buy = df["system_signal"].isin(["strong_buy", "buy"])
    avoid = df["system_signal"].isin(["avoid", "strong_avoid"])
    df.loc[buy & scoreable, "system_correct"] = df.loc[buy & scoreable, "was_good_buy"]
    df.loc[avoid & scoreable, "system_correct"] = ~df.loc[avoid & scoreable, "was_good_buy"]
    df.loc[~scoreable, "system_correct"] = None
    return df


def _load_actual_season(season: int) -> pd.DataFrame:
    """Load actual season results via get_seasonal_stats (PBP fallback)."""
    return get_seasonal_stats(season)


async def _load_historical_prices(
    session: AsyncSession, season: int,
) -> tuple[dict[str, float], str]:
    """Load historical auction prices for `season`.

    Sources are tried in descending order of trustworthiness, and the one actually used
    is reported in the label so a run can never quietly grade itself against the wrong
    number:
      1. league_auction_history, matched by player_name
      2. league_auction_history, matched by player_id
      3. market_value_historic, the season-keyed price reference
      4. nothing — caller falls back to market_value_league, which is CURRENT-season ADP
         and makes every signal metric meaningless

    Returns (name_to_price dict, source_label). A source must supply >= 50 players to
    win; below that the sample cannot support the buy/avoid rates the caller computes.
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

    # market_value_historic — the season-keyed price reference the league-sync path
    # writes. On this deployment it holds the REAL 2025 auction: 159 players summing to
    # $2340 against a 12 x $200 = $2400 budget (97.5% spend), 38 players at exactly $1,
    # and a 24-QB count that is exactly 12 teams x 2. Consensus AAV cannot produce those
    # signatures. league_auction_history is empty here, so without this branch the
    # backtest silently fell through to current-season ADP and scored every signal
    # against the wrong number.
    result3 = await session.execute(
        select(
            Player.name,
            func.avg(MarketValueHistoric.price).label("avg_price"),
        )
        .join(Player, MarketValueHistoric.player_id == Player.id)
        .where(
            MarketValueHistoric.season_year == season,
            MarketValueHistoric.price > 0,
        )
        .group_by(Player.name)
    )
    for row in result3.fetchall():
        if row.name not in history_prices:
            history_prices[row.name] = float(row.avg_price)

    if len(history_prices) >= 50:
        return history_prices, f"market_value_historic ({season}, N={len(history_prices)})"

    # Fallback: use market_value_league from players table
    # This may contain current ADP data rather than historical prices —
    # flag this clearly in the output
    return {}, "market_value_league (fallback — auction history insufficient)"


async def run_backtest(
    session: AsyncSession, season: int | None = None,
    fair_value_per_dollar: float = FAIR_VALUE_PPR_PER_DOLLAR,
) -> tuple[BacktestMetrics, pd.DataFrame]:
    """Run the backtest and return (metrics, player_df).

    Read-only: never writes to any table. `fair_value_per_dollar` is the buy/avoid
    value-per-dollar bar (default = PPR 3.8); pass a format-appropriate bar for non-PPR
    signal scoring. The projection/distribution metrics do not depend on it.
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
        # Provisional, on the legacy flat value-per-dollar bar. OVERWRITTEN below by
        # apply_price_relative_outcome() once the whole population is known — a
        # rank-against-draft-slot measure needs every priced player, so it cannot be
        # computed inside this per-player loop. Kept here so `was_good_buy_flat_vpd`
        # can be reported alongside for comparison.
        was_good_buy = actual_vpd is not None and actual_vpd >= fair_value_per_dollar

        # Only compute signals for players with a meaningful price
        if price > 0:
            system_signal = derive_system_signal(
                value_assessment=player.value_assessment,
                pay_up_flag=bool(player.pay_up_flag),
                value_gap=value_gap,
                ai_ceiling=ai_ceiling,
                league_price=price,
                projected_ppr=proj_ppr,
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
    df = apply_price_relative_outcome(df)

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


# ═══════════════════════════════════════════════════════════════════════════
# FORMAT-AWARE evaluation harness — projection curve + tier-calibration scoring.
# READ-ONLY. It MEASURES the projection against real actuals; it never fits or
# optimizes a transform toward any target dollar value or tier count. The two
# downstream builds (projection reshape, distribution-relative tiers) score
# themselves against these measurements — the harness itself has no free knobs.
# ═══════════════════════════════════════════════════════════════════════════

# Points-per-reception by format. Non-PPR points = ppr - (1 - rp) * receptions.
_RECEPTION_POINTS = {"ppr": 1.0, "half_ppr": 0.5, "standard": 0.0}
_RANKS = [1, 3, 5, 10, 20, 30, 40, 50, 60]
_SKILL = ("QB", "RB", "WR", "TE")
ALL_FORMATS = ("ppr", "half_ppr", "standard")


def reception_data_available(actuals: pd.DataFrame) -> bool:
    """True if a season's actuals carry a reception breakdown (required for non-PPR).
    2023/2024 hold PPR totals + games only; only 2025 (PBP-derived) has receptions."""
    return "receptions" in actuals.columns


def actual_points_for_format(ppr_points: float, receptions, fmt: str) -> float | None:
    """Actual points in `fmt`. Returns None for non-PPR when receptions are unavailable —
    the harness must SAY a format is uncomputable for a season, never score it on PPR data."""
    if fmt == "ppr":
        return ppr_points
    if receptions is None or (isinstance(receptions, float) and pd.isna(receptions)):
        return None
    return ppr_points - (1.0 - _RECEPTION_POINTS[fmt]) * float(receptions)


def _finish_at_ranks(values: list[float]) -> dict[int, float | None]:
    v = sorted([x for x in values if x and x > 0], reverse=True)
    return {r: (round(v[r - 1], 1) if len(v) >= r else None) for r in _RANKS}


def spread_ratio(actual_finish: dict, proj_finish: dict, hi: int = 5, lo: int = 50) -> float | None:
    """actual spread ÷ projected spread between rank `hi` and `lo`. >1 == projections are
    compressed vs reality (the measured flatness). A pure shape measure — no player matching."""
    a_hi, a_lo, p_hi, p_lo = actual_finish.get(hi), actual_finish.get(lo), proj_finish.get(hi), proj_finish.get(lo)
    if None in (a_hi, a_lo, p_hi, p_lo) or (p_hi - p_lo) == 0:
        return None
    return round((a_hi - a_lo) / (p_hi - p_lo), 2)


def separator_count(values: list[float], topn: int = 12) -> dict | None:
    """Largest gap in the top-`topn` (sorted desc) → count above the break + gap size.
    This is the gap method tier candidates get scored against (B4)."""
    v = sorted([x for x in values if x and x > 0], reverse=True)[:topn]
    if len(v) < 3:
        return None
    gaps = [(v[i] - v[i + 1], i + 1) for i in range(len(v) - 1)]
    b = max(gaps, key=lambda g: g[0])
    return {"count": b[1], "gap": round(b[0], 1)}


async def run_format_backtest(
    session: AsyncSession,
    seasons: list[int],
    formats: tuple[str, ...] = ALL_FORMATS,
    holdout: int | None = None,
) -> dict:
    """Format-aware, read-only measurement of the CURRENT projection vs historical actuals.

    Returns a structured dict (see CLI for rendering). For each format it reports, per season:
      * availability (False when the season lacks a reception breakdown for non-PPR)
      * player-level matched metrics per position: n, mae, bias, corr, within20
      * distribution shape per position: projected-vs-actual finish at ranks (rank_delta),
        spread_ratio, and separator_count
    `holdout` seasons are computed but LABELLED separately so a transform fit on the reference
    seasons can be verified on held-out data. This function fits nothing.
    """
    from backend.engines.valuation import _extract_ppr  # local import: engine dep, avoid cycle
    import statistics

    await session.execute(text("SET TRANSACTION READ ONLY"))

    proj_rows = (await session.execute(
        select(Player, PlayerProfile)
        .join(PlayerProfile, Player.id == PlayerProfile.player_id, isouter=True)
        .where(Player.position.in_(list(_SKILL)))
    )).fetchall()

    # current projected points per format per player (with match keys)
    proj: dict[str, list] = {f: [] for f in formats}
    for player, profile in proj_rows:
        if not profile or not profile.clean_season_baseline:
            continue
        gsis = (player.yahoo_player_id or "").replace("nfl_", "") or None
        nm = player.name.lower()
        for f in formats:
            pts = _extract_ppr(profile, f)
            if pts and pts > 0:
                proj[f].append((player.position, float(pts), gsis, nm))

    out = {
        "seasons": seasons,
        "reference_seasons": [s for s in seasons if s != holdout],
        "holdout": holdout,
        "formats": {},
    }

    for f in formats:
        proj_finish = {pos: _finish_at_ranks([p for (po, p, _, _) in proj[f] if po == pos]) for pos in _SKILL}
        fmt_out = {"projected_finish": proj_finish, "availability": {}, "by_season": {}}
        for sea in seasons:
            actuals = _load_actual_season(sea)
            has_rec = reception_data_available(actuals)
            available = (f == "ppr") or has_rec
            fmt_out["availability"][sea] = available
            if not available:
                continue
            name_col = "player_display_name" if "player_display_name" in actuals.columns else "player_name"
            actual_by_id: dict[str, dict] = {}
            actual_by_name: dict[str, dict] = {}
            by_pos: dict[str, list] = {pos: [] for pos in _SKILL}
            for _, r in actuals.iterrows():
                ppr = float(r["fantasy_points_ppr"] or 0)
                rec = r["receptions"] if has_rec else None
                pts = actual_points_for_format(ppr, rec, f)
                if pts is None:
                    continue
                ent = {"pts": pts, "games": int(r.get("games") or 0)}
                actual_by_id[str(r["player_id"])] = ent
                actual_by_name[str(r[name_col]).lower()] = ent
                pos = r.get("position")
                if pos in by_pos:
                    by_pos[pos].append(pts)

            actual_finish = {pos: _finish_at_ranks(by_pos[pos]) for pos in _SKILL}
            seps = {pos: separator_count(by_pos[pos]) for pos in _SKILL}

            player_metrics: dict[str, dict] = {}
            shape: dict[str, dict] = {}
            for pos in _SKILL:
                pairs = []
                for (po, pts, gsis, nm) in proj[f]:
                    if po != pos:
                        continue
                    a = (actual_by_id.get(gsis) if gsis else None) or actual_by_name.get(nm)
                    if a:
                        pairs.append((pts, a["pts"]))
                if len(pairs) >= 5:
                    errs = [p - a for p, a in pairs]
                    ae = [abs(e) for e in errs]
                    ps = pd.Series([p for p, _ in pairs])
                    as_ = pd.Series([a for _, a in pairs])
                    try:
                        corr = round(float(ps.corr(as_)), 3)
                    except Exception:
                        corr = None
                    within = round(sum(1 for (p, a) in pairs if a > 0 and abs(p - a) <= 0.2 * abs(a)) / len(pairs) * 100, 1)
                    player_metrics[pos] = {
                        "n": len(pairs), "mae": round(statistics.mean(ae), 1),
                        "bias": round(statistics.mean(errs), 1), "corr": corr, "within20": within,
                    }
                delta = {
                    r: (round(proj_finish[pos][r] - actual_finish[pos][r])
                        if (proj_finish[pos][r] is not None and actual_finish[pos][r] is not None) else None)
                    for r in _RANKS
                }
                shape[pos] = {
                    "spread_ratio": spread_ratio(actual_finish[pos], proj_finish[pos]),
                    "rank_delta": delta,
                    "actual_finish": actual_finish[pos],
                }
            fmt_out["by_season"][sea] = {"separators": seps, "player_metrics": player_metrics, "shape": shape}
        out["formats"][f] = fmt_out
    return out
