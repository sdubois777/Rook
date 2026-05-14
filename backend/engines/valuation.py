"""
Stage 9: Draft Bible Valuation Pass

Pure Python computation — no AI calls.

Synthesizes all pre-draft agent outputs (PlayerProfile, PlayerInjuryProfile)
into final valuation fields on the players table:
  - tier (1-5 per position)
  - baseline_value (PPR points → auction dollars via PAR method)
  - risk_adjusted_value (baseline × (1 + risk_modifier))
  - recommended_bid_ceiling (two-value formula from ARCHITECTURE.md)
  - let_go_threshold (bid ceiling × risk-adjusted multiplier)
  - value_gap and value_gap_signal (system vs market gap)

Formulas from docs/ARCHITECTURE.md — Two-Value Auction System:

  Risk is applied as a discount to market_value BEFORE blending,
  not as a multiplier on the final ceiling.

  risk_adjusted_market = market_value × (1 - RISK_MARKET_DISCOUNT[risk_level])

  All tiers:
    blend = system_value × (1 - anchor_weight) + risk_adjusted_market × anchor_weight
    ceiling = blend × positional_scarcity_modifier (T1 only)

  let_go_threshold = ceiling × LET_GO_MULTIPLIER[risk_level]

Anchor weights (market weight per tier):
  T1=0.85, T2=0.65, T3=0.40, T4=0.15, T5=0.00
Scarcity:       T1 RB=1.35, T1 WR=1.20, T1 QB/TE=1.10
Risk discounts:  low=0%, moderate=8%, high=15%, volatile=22%
Let-go:          low=1.20×, moderate=1.15×, high=1.10×, volatile=1.05×
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile
from backend.models.league_config import LeagueConfig, DEFAULT_LEAGUE_CONFIG
from backend.utils.seasons import get_analysis_year

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# League defaults — derived from LeagueConfig
# ---------------------------------------------------------------------------

# These module-level constants preserved for backward compatibility with
# functions that accept them as kwargs. All new code should use LeagueConfig.
LEAGUE_SKILL_BUDGET = int(DEFAULT_LEAGUE_CONFIG.budget * DEFAULT_LEAGUE_CONFIG.skill_budget_pct)
LEAGUE_TEAMS        = DEFAULT_LEAGUE_CONFIG.team_count
LEAGUE_SKILL_DOLLAR_POOL = DEFAULT_LEAGUE_CONFIG.total_skill_pool

# Positional budget allocation targets (% of LEAGUE_SKILL_DOLLAR_POOL)
# From LEAGUE_RULES.md: RB=38%, WR=32%, QB=10%, TE=10%
# Do NOT invert WR and QB. QB is 10%, not 38%.
POSITION_BUDGET_SHARE: dict[str, float] = {
    "QB": 0.10,
    "RB": 0.38,
    "WR": 0.32,
    "TE": 0.10,
}

# Maximum realistic bid per position — hard cap enforced (not just logged)
# per LEAGUE_RULES.md Rule #1 and #3
MAX_REALISTIC_BID: dict[str, int] = {
    "RB": 80,
    "WR": 70,
    "QB": 50,
    "TE": 45,
    "K":   2,
    "DEF": 2,
}

# Minimum replacement-level PPR per game — sanity floor for dynamic computation.
# If the dynamically computed replacement PPR/game falls below these values,
# something is wrong with the data (too few profiles, skewed sample).
REPLACEMENT_LEVEL_PPR_PER_GAME: dict[str, float] = {
    "QB": 18.0,
    "RB": 8.0,
    "WR": 7.0,
    "TE": 5.0,
}

# Maximum replacement-level PPR per game — prevents over-compression when
# profile data inflates bench player projections above realistic levels.
REPLACEMENT_LEVEL_MAX_PPR_PER_GAME: dict[str, float] = {
    "QB": 22.0,   # ~374 season — streamable QB ceiling
    "RB": 10.0,   # ~170 season — waiver wire RB2 ceiling
    "WR": 9.0,    # ~153 season — waiver wire WR3 ceiling
    "TE": 7.0,    # ~119 season — streamable TE ceiling
}

# Injury recovery discount applied to PPR baseline for players with major injuries
POST_MAJOR_INJURY_DISCOUNT = 0.75  # 25% discount

# Default roster slots for a standard Yahoo PPR league
DEFAULT_ROSTER_SLOTS: dict[str, int] = {
    "QB": 1, "RB": 2, "WR": 2, "FLEX": 1, "TE": 1,
    "K": 1, "DEF": 1, "BENCH": 7,
}

# FLEX and bench allocation splits by position (empirical auction norms)
_FLEX_SPLIT: dict[str, float] = {"RB": 0.30, "WR": 0.60, "TE": 0.10}
_BENCH_SPLIT: dict[str, float] = {"QB": 0.08, "RB": 0.28, "WR": 0.35, "TE": 0.14}

# Draftable positions for this pass
DRAFTABLE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})

# ---------------------------------------------------------------------------
# Tier assignment
# ---------------------------------------------------------------------------

# Tier boundaries by positional rank (1-indexed, inclusive upper bound)
_TIER_CUTOFFS = [3, 9, 19, 34]  # T1≤3, T2≤9, T3≤19, T4≤34, T5=rest


def assign_tier(rank: int) -> int:
    """Return tier 1-5 for a player ranked `rank` among their position."""
    for tier, cutoff in enumerate(_TIER_CUTOFFS, start=1):
        if rank <= cutoff:
            return tier
    return 5


# ---------------------------------------------------------------------------
# Anchor weights and scarcity modifiers
# ---------------------------------------------------------------------------

ANCHOR_WEIGHTS: dict[int, Decimal] = {
    1: Decimal("0.85"),
    2: Decimal("0.65"),
    3: Decimal("0.40"),
    4: Decimal("0.15"),
    5: Decimal("0.00"),
}

SCARCITY_MODIFIERS: dict[str, Decimal] = {
    "RB": Decimal("1.35"),
    "WR": Decimal("1.20"),
    "QB": Decimal("1.10"),
    "TE": Decimal("1.10"),
}

# Risk market discount — applied to market_value BEFORE blending.
# Higher risk = larger discount to what the room is willing to pay.
# This replaces the old approach of multiplying risk_modifier on the final ceiling,
# which crushed elite injured players into undraftable territory.
RISK_MARKET_DISCOUNT: dict[str, Decimal] = {
    "low":      Decimal("0.00"),
    "moderate": Decimal("0.08"),
    "high":     Decimal("0.15"),
    "volatile": Decimal("0.22"),
}

# Let-go threshold multiplier — risk-adjusted walk-away price above ceiling.
# Low risk = willing to stretch (1.20×), volatile = tight leash (1.05×).
LET_GO_MULTIPLIER: dict[str, Decimal] = {
    "low":      Decimal("1.20"),
    "moderate": Decimal("1.15"),
    "high":     Decimal("1.10"),
    "volatile": Decimal("1.05"),
}

# ---------------------------------------------------------------------------
# Value gap thresholds
# ---------------------------------------------------------------------------

VALUE_GAP_OVERVALUE_THRESHOLD  = Decimal("-5")   # gap < -5  → market_overvalues
VALUE_GAP_UNDERVALUE_THRESHOLD = Decimal("5")    # gap > 5   → market_undervalues

# ---------------------------------------------------------------------------
# Pure computation functions (stateless — easy to unit test)
# ---------------------------------------------------------------------------


def ppr_to_system_value(
    ppr_points: float,
    replacement_ppr: float,
    total_par: float,
    position_budget: float,
) -> Decimal:
    """
    Convert PPR points to auction-dollar system_value via Points Above Replacement.

    Args:
        ppr_points:       Player's projected clean-season PPR total.
        replacement_ppr:  PPR of the player at the replacement rank cutoff.
        total_par:        Sum of PAR for all draftable players at this position.
        position_budget:  Total auction dollars allocated to this position group.

    Returns:
        Decimal system_value in dollars (minimum $1).
    """
    par = max(0.0, ppr_points - replacement_ppr)
    if total_par <= 0 or par <= 0:
        return Decimal("1.00")
    raw = (par / total_par) * position_budget
    return _to_dec(max(1.0, round(raw, 2)))


def compute_bid_ceiling(
    system_value: Decimal,
    market_value: Optional[Decimal],
    tier: int,
    position: str,
    risk_level: str = "low",
) -> Decimal:
    """
    Compute the recommended bid ceiling using the two-value formula.

    Risk is applied as a discount to market_value BEFORE blending, not as a
    multiplier on the final ceiling. This prevents elite injured players from
    becoming undraftable (e.g., Amon-Ra $16 ceiling on $49 market).

    Args:
        system_value: PAR-derived auction dollar value.
        market_value: Consensus market price (None = use system_value).
        tier: Player tier (1-5).
        position: Position string (QB, RB, WR, TE).
        risk_level: Injury risk level (low/moderate/high/volatile).

    Returns:
        Decimal bid ceiling in dollars (minimum $1).
    """
    mv = market_value if market_value is not None else system_value
    discount = RISK_MARKET_DISCOUNT.get(risk_level, Decimal("0.00"))
    risk_adjusted_market = mv * (Decimal("1") - discount)

    anchor = ANCHOR_WEIGHTS.get(tier, Decimal("0.00"))
    blend = system_value * (Decimal("1") - anchor) + risk_adjusted_market * anchor

    if tier == 1:
        scarcity = SCARCITY_MODIFIERS.get(position, Decimal("1.00"))
        ceiling = blend * scarcity
    else:
        ceiling = blend

    return _to_dec(max(Decimal("1.00"), ceiling))


def get_market_context(player) -> dict:
    """
    Build market context combining league auction history and FP consensus.

    Returns:
        {market_value_league, market_value_fantasypros, league_bias,
         league_bias_signal, effective_market_value}
    """
    league = player.market_value_league
    fp = player.market_value_fantasypros or player.market_value
    effective = league if (league is not None and league > 0) else fp

    bias = None
    bias_signal = None
    if league is not None and fp is not None and fp > 0:
        bias = _to_dec(league - fp)
        if bias > Decimal("5"):
            bias_signal = "league_overpays"
        elif bias < Decimal("-5"):
            bias_signal = "league_underpays"
        else:
            bias_signal = "league_aligned"

    return {
        "market_value_league": league,
        "market_value_fantasypros": fp,
        "league_bias": bias,
        "league_bias_signal": bias_signal,
        "effective_market_value": effective,
    }


def compute_value_gap(
    system_value: Decimal,
    market_value: Optional[Decimal],
) -> tuple[Optional[Decimal], Optional[str]]:
    """
    Compute value_gap (system_value - market_value) and value_gap_signal.

    Returns (None, None) when market_value is not available.
    """
    if market_value is None:
        return None, None

    gap = system_value - market_value
    gap = _to_dec(gap)

    if gap < VALUE_GAP_OVERVALUE_THRESHOLD:
        signal = "market_overvalues"
    elif gap > VALUE_GAP_UNDERVALUE_THRESHOLD:
        signal = "market_undervalues"
    else:
        signal = "aligned"

    return gap, signal


def compute_value_gap_from_player(player) -> tuple[Optional[Decimal], Optional[str]]:
    """
    Compute value_gap_signal using the best available system estimate.

    Priority: ai_bid_ceiling > recommended_bid_ceiling > baseline_value.
    ai_bid_ceiling is the authoritative calibrated estimate from the AI
    valuation agent. baseline_value (PAR math) is floored at $1 for many
    players, making it unreliable for gap detection.

    Market source: market_value_league > market_value_fantasypros.
    """
    market = (
        getattr(player, "market_value_league", None)
        or getattr(player, "market_value_fantasypros", None)
    )
    if not market:
        return None, "no_market_data"

    market = _to_dec(market)

    # Best available system estimate
    system_estimate = None
    for attr in ("ai_bid_ceiling", "recommended_bid_ceiling", "baseline_value"):
        val = getattr(player, attr, None)
        if val is not None and float(val) > 0:
            system_estimate = _to_dec(val)
            break

    if system_estimate is None:
        return None, "no_system_data"

    gap = system_estimate - market

    if gap < VALUE_GAP_OVERVALUE_THRESHOLD:
        signal = "market_overvalues"
    elif gap > VALUE_GAP_UNDERVALUE_THRESHOLD:
        signal = "market_undervalues"
    else:
        signal = "aligned"

    return gap, signal


def compute_let_go_threshold(bid_ceiling: Decimal, risk_level: str = "low") -> Decimal:
    """Let-go threshold — risk-adjusted walk-away price above ceiling.

    Low risk = 1.20x (willing to stretch), volatile = 1.05x (tight leash).
    """
    multiplier = LET_GO_MULTIPLIER.get(risk_level, Decimal("1.20"))
    return _to_dec(bid_ceiling * multiplier)


def _to_dec(value: float | Decimal) -> Decimal:
    """Normalize to Decimal with 2dp."""
    return Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Dynamic pool sizes and replacement levels (FIX 1 + FIX 2)
# ---------------------------------------------------------------------------


def get_draftable_pool_sizes(
    teams: int = DEFAULT_LEAGUE_CONFIG.team_count,
    roster_slots: dict | None = None,
) -> dict[str, int]:
    """
    Calculate how many players at each position realistically get drafted.

    Formula per position:
      starters = roster_slots[position] × team_count
      + flex allocation (split 60% WR, 30% RB, 10% TE)
      + bench depth allocation

    Returns dict like {"QB": 19, "RB": 52, "WR": 60, "TE": 25}.
    """
    slots = roster_slots or DEFAULT_ROSTER_SLOTS

    qb_starters = slots.get("QB", 1) * teams
    rb_starters = slots.get("RB", 2) * teams
    wr_starters = slots.get("WR", 2) * teams
    te_starters = slots.get("TE", 1) * teams

    flex = slots.get("FLEX", 1)
    bench = slots.get("BENCH", 7)

    rb_flex = round(flex * teams * _FLEX_SPLIT["RB"])
    wr_flex = round(flex * teams * _FLEX_SPLIT["WR"])
    te_flex = round(flex * teams * _FLEX_SPLIT["TE"])

    qb_bench = round(bench * teams * _BENCH_SPLIT["QB"])
    rb_bench = round(bench * teams * _BENCH_SPLIT["RB"])
    wr_bench = round(bench * teams * _BENCH_SPLIT["WR"])
    te_bench = round(bench * teams * _BENCH_SPLIT["TE"])

    return {
        "QB": qb_starters + qb_bench,
        "RB": rb_starters + rb_flex + rb_bench,
        "WR": wr_starters + wr_flex + wr_bench,
        "TE": te_starters + te_flex + te_bench,
    }


def calculate_replacement_level(
    sorted_pprs: list[float],
    pool_size: int,
) -> float:
    """
    Replacement level = projected PPR of the last player in the draftable pool.

    Args:
        sorted_pprs: PPR values sorted descending.
        pool_size: Number of players drafted at this position.

    Returns the replacement-level PPR (season total).
    """
    if not sorted_pprs:
        return 0.0
    if len(sorted_pprs) >= pool_size:
        return sorted_pprs[pool_size - 1]
    return sorted_pprs[-1]


def sanity_check_valuations(
    valued_players: list,
    league_pool: float = DEFAULT_LEAGUE_CONFIG.total_skill_pool,
) -> list[str]:
    """
    Post-valuation sanity checks. Returns list of warning strings.
    Empty list = all checks passed.

    Args:
        valued_players: Player objects with baseline_value set.
        league_pool: Expected total dollar pool.
    """
    warnings: list[str] = []

    by_pos: dict[str, list[float]] = {}
    total = 0.0
    for p in valued_players:
        val = float(p.baseline_value or 0)
        by_pos.setdefault(p.position, []).append(val)
        total += val

    # Check 1: Total value shouldn't exceed pool by >10%
    if total > league_pool * 1.10:
        warnings.append(
            f"Total system value ${total:.0f} exceeds "
            f"pool ${league_pool:.0f} by >10%"
        )

    # Check 2: No position's max should exceed position cap
    for pos, values in by_pos.items():
        max_val = max(values) if values else 0
        cap = MAX_REALISTIC_BID.get(pos, 80)
        if max_val > cap:
            warnings.append(
                f"Max {pos} value ${max_val:.0f} exceeds cap ${cap}"
            )

    # Check 3: Reasonable number of players > $10
    above_10 = sum(1 for vals in by_pos.values() for v in vals if v > 10)
    if above_10 < 50 or above_10 > 140:
        warnings.append(
            f"Unusual distribution: {above_10} players "
            f"above $10 (expected 50-140)"
        )

    # Check 4: Average values per position should be reasonable
    # Averages include many $1 players below replacement, so lower bound is low
    expected_avg: dict[str, tuple[float, float]] = {
        "QB": (3, 25), "RB": (5, 25),
        "WR": (4, 22), "TE": (3, 22),
    }
    for pos, values in by_pos.items():
        if not values or pos not in expected_avg:
            continue
        avg = sum(values) / len(values)
        lo, hi = expected_avg[pos]
        if not (lo <= avg <= hi):
            warnings.append(
                f"{pos} average ${avg:.1f} outside expected range ${lo}-${hi}"
            )

    return warnings


# ---------------------------------------------------------------------------
# Async valuation pass — loads data, computes, writes back
# ---------------------------------------------------------------------------


async def run_valuation_pass(
    config: LeagueConfig = DEFAULT_LEAGUE_CONFIG,
) -> dict:
    """
    Load all players with profiles, compute valuations, write back to DB.

    Uses config.total_skill_pool as the total calibration pool
    per docs/rules/LEAGUE_RULES.md Rule #3.

    Args:
        config: LeagueConfig with team_count, budget, scoring etc.
                Defaults to DEFAULT_LEAGUE_CONFIG (12 teams, $200, PPR).

    Returns:
        Summary dict: {processed, updated, skipped, analysis_year}.
    """
    analysis_year = get_analysis_year()
    total_budget = config.total_skill_pool
    league_teams = config.team_count

    async with AsyncSessionLocal() as session:
        # Eager-load profiles and injury profiles — one query, no N+1
        stmt = (
            select(Player)
            .options(
                selectinload(Player.profile),
                selectinload(Player.injury_profile),
                selectinload(Player.dependencies),
            )
        )
        players: list[Player] = (await session.execute(stmt)).scalars().all()

        # --------------- Group by position, extract ppr_points ---------------
        # Store (player, raw_ppr, adjusted_ppr) tuples.
        # raw_ppr: used for tier ranking (talent/role, never affected by risk)
        # adjusted_ppr: used for dollar value conversion (reflects risk and dependencies)
        pos_groups: dict[str, list[tuple[Player, float, float]]] = {
            p: [] for p in DRAFTABLE_POSITIONS
        }
        valued_player_ids: set = set()

        for player in players:
            pos = player.position
            if pos not in DRAFTABLE_POSITIONS:
                continue
            # Free agents (team_abbr="FA" or None) are undraftable — skip.
            # They'll be cleared by the stale-value sweep below.
            if not player.team_abbr or player.team_abbr == "FA":
                continue
            raw_ppr = _extract_ppr(player.profile)
            if raw_ppr <= 0:
                continue
            # Adjusted PPR: apply injury discount and dependency adjustments
            # for dollar value conversion only — never affects tier ranking
            adjusted_ppr = raw_ppr
            adjusted_ppr = _apply_injury_discount(adjusted_ppr, player.injury_profile, player.profile)
            if adjusted_ppr > 0:
                adjusted_ppr = _apply_dependency_adjustment(adjusted_ppr, player.dependencies)
            adjusted_ppr = max(0.0, adjusted_ppr)
            pos_groups[pos].append((player, raw_ppr, adjusted_ppr))

        # Sort each group descending by RAW PPR — tier is about talent, not risk
        for pos in pos_groups:
            pos_groups[pos].sort(key=lambda x: x[1], reverse=True)

        # --------------- Dynamic pool sizes + replacement levels ---------------
        pool_sizes = get_draftable_pool_sizes(league_teams)

        par_context: dict[str, dict] = {}
        for pos, group in pos_groups.items():
            pool_size = pool_sizes.get(pos, len(group))
            # Use adjusted_ppr (x[2]) for PAR calculations — dollar values reflect risk
            sorted_pprs = [adj_ppr for _, _, adj_ppr in group]
            dynamic_repl = calculate_replacement_level(sorted_pprs, pool_size)

            # Enforce replacement level bounds (PPR/game × 17 games)
            floor_ppr = REPLACEMENT_LEVEL_PPR_PER_GAME.get(pos, 0.0) * 17
            max_ppr = REPLACEMENT_LEVEL_MAX_PPR_PER_GAME.get(pos, 15.0) * 17
            repl_ppr = min(max(dynamic_repl, floor_ppr), max_ppr)
            if repl_ppr > dynamic_repl and repl_ppr == floor_ppr:
                repl_name = "?"
                if len(sorted_pprs) >= pool_size:
                    # Find the replacement player's name for logging
                    repl_name = group[pool_size - 1][0].name
                logger.info(
                    "%s replacement floor enforced: dynamic=%.1f "
                    "(#%d %s) < floor=%.1f (%.1f PPG × 17)",
                    pos, dynamic_repl, pool_size, repl_name,
                    floor_ppr, REPLACEMENT_LEVEL_PPR_PER_GAME[pos],
                )
            if dynamic_repl > max_ppr:
                logger.info(
                    "%s replacement cap enforced: dynamic=%.1f > max=%.1f (%.1f PPG × 17)",
                    pos, dynamic_repl, max_ppr, REPLACEMENT_LEVEL_MAX_PPR_PER_GAME[pos],
                )

            total_par = sum(max(0.0, adj_ppr - repl_ppr) for _, _, adj_ppr in group)
            pos_budget = total_budget * POSITION_BUDGET_SHARE[pos]

            par_context[pos] = {
                "replacement_ppr": repl_ppr,
                "total_par":       total_par,
                "position_budget": pos_budget,
                "pool_size":       pool_size,
            }

            logger.info(
                "PAR context %s: pool=%d, repl=%.1f PPR (#%d of %d players), "
                "total_par=%.1f, budget=$%.0f",
                pos, pool_size, repl_ppr,
                min(pool_size, len(group)), len(group),
                total_par, pos_budget,
            )

        # --------------- Compute and write valuations ------------------------
        processed = 0
        updated   = 0
        skipped   = 0

        for pos, group in pos_groups.items():
            ctx = par_context[pos]
            for rank_0, (player, raw_ppr, adjusted_ppr) in enumerate(group):
                rank = rank_0 + 1
                tier = assign_tier(rank)  # from RAW PPR rank — talent, not risk

                sv = ppr_to_system_value(
                    ppr_points        = adjusted_ppr,  # dollar value from risk-adjusted PPR
                    replacement_ppr   = ctx["replacement_ppr"],
                    total_par         = ctx["total_par"],
                    position_budget   = ctx["position_budget"],
                )

                risk_level = "low"
                if player.injury_profile and player.injury_profile.overall_risk_level:
                    risk_level = player.injury_profile.overall_risk_level

                rm = _get_risk_modifier(player.injury_profile)

                # Use effective_market_value (league price if available, FP fallback)
                mctx = get_market_context(player)
                effective_mv = mctx["effective_market_value"]

                ceiling  = compute_bid_ceiling(sv, effective_mv, tier, pos, risk_level)

                # FIX 5: Hard cap enforcement — cap ceiling to MAX_REALISTIC_BID
                max_bid = MAX_REALISTIC_BID.get(pos, 80)
                max_bid_dec = Decimal(str(max_bid))
                if ceiling > max_bid_dec:
                    logger.info(
                        "BID CEILING CAPPED: %s (%s T%d) ceiling=$%s → $%d max. "
                        "sv=$%s, total_par=%.1f, pool=$%.0f",
                        player.name, pos, tier, ceiling, max_bid,
                        sv, ctx["total_par"], ctx["position_budget"],
                    )
                    ceiling = max_bid_dec

                let_go   = compute_let_go_threshold(ceiling, risk_level)
                risk_adj = _to_dec(sv * (Decimal("1") + (rm or Decimal("0"))))
                anchor   = ANCHOR_WEIGHTS.get(tier, Decimal("0.00"))
                scarcity = SCARCITY_MODIFIERS.get(pos, Decimal("1.00")) if tier == 1 else Decimal("1.00")

                # Compute ceiling/floor dollar values from upside/downside PPR
                upside_ppr, downside_ppr = _extract_upside_downside(player.profile)
                ceiling_val = None
                floor_val = None
                if upside_ppr > 0:
                    ceiling_val = ppr_to_system_value(
                        upside_ppr, ctx["replacement_ppr"],
                        ctx["total_par"], ctx["position_budget"],
                    )
                if downside_ppr > 0:
                    floor_val = ppr_to_system_value(
                        downside_ppr, ctx["replacement_ppr"],
                        ctx["total_par"], ctx["position_budget"],
                    )

                # Update in-session player object — set values BEFORE gap
                # so compute_value_gap_from_player sees current ceiling
                player.tier                       = tier
                player.baseline_value             = sv
                player.ceiling_value              = ceiling_val
                player.floor_value                = floor_val
                player.risk_adjusted_value        = _to_dec(max(Decimal("1.00"), risk_adj))
                player.recommended_bid_ceiling    = ceiling
                player.let_go_threshold           = let_go
                player.elite_anchor_weight        = anchor
                player.positional_scarcity_modifier = scarcity

                # Value gap: uses ai_bid_ceiling > rec_ceiling > baseline
                gap, sig = compute_value_gap_from_player(player)
                player.value_gap                  = gap
                player.value_gap_signal           = sig
                player.data_confidence            = _confidence(player)

                session.add(player)
                valued_player_ids.add(player.id)
                processed += 1
                updated   += 1

        # Clear stale valuations for players that were skipped (no profile or
        # below usage threshold). This prevents ghost values from previous runs.
        cleared = 0
        for player in players:
            if player.position in DRAFTABLE_POSITIONS and player.id not in valued_player_ids:
                if player.baseline_value is not None:
                    player.tier                       = None
                    player.baseline_value             = None
                    player.risk_adjusted_value        = None
                    player.recommended_bid_ceiling    = None
                    player.let_go_threshold           = None
                    player.elite_anchor_weight        = None
                    player.positional_scarcity_modifier = None
                    player.value_gap                  = None
                    player.value_gap_signal           = None
                    player.data_confidence            = "low"
                    session.add(player)
                    cleared += 1
                skipped += 1

        # --------------- Sanity check before commit ----------------------------
        valued_list = [p for p, *_ in
                       (item for group in pos_groups.values() for item in group)
                       if p.id in valued_player_ids]
        warnings = sanity_check_valuations(valued_list, float(total_budget))
        for w in warnings:
            logger.warning("SANITY CHECK: %s", w)

        await session.commit()

    logger.info(
        "Valuation pass (%d): %d updated, %d skipped, %d cleared, analysis_year=%d",
        processed, updated, skipped, cleared, analysis_year,
    )
    return {
        "processed":     processed,
        "updated":       updated,
        "skipped":       skipped,
        "cleared":       cleared,
        "analysis_year": analysis_year,
        "pool_sizes":    pool_sizes,
        "replacement_levels": {
            pos: ctx["replacement_ppr"] for pos, ctx in par_context.items()
        },
        "warnings":      warnings,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_ppr(profile: Optional[PlayerProfile]) -> float:
    """Extract PPR from clean_season_baseline JSONB, or 0.

    Prefers projected_ppr_season (forward-looking Sonnet projection) over
    ppr_points (historical baseline).  Falls back to ppr_points when no
    projection exists (Haiku/Python-only profiles).
    """
    if not profile or not profile.clean_season_baseline:
        return 0.0
    baseline = profile.clean_season_baseline
    val = baseline.get("projected_ppr_season") or baseline.get("ppr_points", 0)
    try:
        return max(0.0, float(val or 0))
    except (TypeError, ValueError):
        return 0.0


def _extract_upside_downside(profile: Optional[PlayerProfile]) -> tuple[float, float]:
    """Extract upside_ppr and downside_ppr from clean_season_baseline, or (0, 0)."""
    if not profile or not profile.clean_season_baseline:
        return 0.0, 0.0
    baseline = profile.clean_season_baseline
    try:
        upside = max(0.0, float(baseline.get("upside_ppr", 0) or 0))
        downside = max(0.0, float(baseline.get("downside_ppr", 0) or 0))
    except (TypeError, ValueError):
        return 0.0, 0.0
    return upside, downside


# Per LEAGUE_RULES.md: volatile = -35% or worse. Cap at -40% absolute maximum.
MAX_RISK_MODIFIER = Decimal("-0.40")


def _get_risk_modifier(injury_profile: Optional[PlayerInjuryProfile]) -> Optional[Decimal]:
    """Return risk_adjusted_value_modifier from injury profile, or None.

    Capped at MAX_RISK_MODIFIER (-0.40) per LEAGUE_RULES.md — no player
    should lose more than 40% of value regardless of injury flag stacking.
    """
    if not injury_profile or injury_profile.risk_adjusted_value_modifier is None:
        return None
    modifier = Decimal(str(injury_profile.risk_adjusted_value_modifier))
    if modifier < MAX_RISK_MODIFIER:
        logger.info(
            "Risk modifier capped: %s → %s",
            modifier, MAX_RISK_MODIFIER,
        )
        modifier = MAX_RISK_MODIFIER
    return modifier


def _apply_injury_discount(
    ppr: float,
    injury_profile: Optional[PlayerInjuryProfile],
    profile: Optional[PlayerProfile],
) -> float:
    """
    Apply injury and decline discounts to PPR baseline.

    Discount sources (applied multiplicatively, capped at 0.60):
    - post_acl_flag:      25% discount (POST_MAJOR_INJURY_DISCOUNT)
    - workload_cliff_flag: 15% discount
    - career_trajectory = "declining": 15% discount (AI model assessment)
    - clean_season_baseline "declining" flag: 15% discount (Python-computed)
    """
    discount = 1.0

    # Check injury profile for major injury flags
    if injury_profile:
        if injury_profile.post_acl_flag:
            discount *= POST_MAJOR_INJURY_DISCOUNT
        elif injury_profile.workload_cliff_flag:
            discount *= 0.85  # 15% discount for workload cliff

    # Check profile for career decline — two sources:
    # 1. AI model's career_trajectory assessment (catches cases like Chubb
    #    where only peak seasons are "clean" but model sees overall decline)
    # 2. Python-computed declining flag in clean_season_baseline
    if profile:
        if profile.career_trajectory == "declining":
            discount *= 0.85  # 15% decline discount
        elif profile.clean_season_baseline and profile.clean_season_baseline.get("declining"):
            discount *= 0.85  # 15% decline discount

    # Floor: never discount more than 40%
    discount = max(discount, 0.60)

    return ppr * discount


def _apply_dependency_adjustment(ppr: float, dependencies: list) -> float:
    """
    Apply pre-draft dependency flag adjustments to projected PPR.

    Rules:
    - BENEFICIARY + departed_team → apply immediately (positive)
    - DISPLACED + active_and_healthy → apply immediately (negative)
    - SCHEME_FIT → half weight pre-draft
    - CONTINGENT, injured/absent BENEFICIARY → skip (live-draft only)
    """
    if not dependencies:
        return ppr

    total_adj = 0.0
    for dep in dependencies:
        flag = dep.flag_type
        trigger = dep.trigger_condition or ""
        impact = float(dep.value_impact_pct or 0)

        # Normalize: AI model outputs whole percentages (35 = 35%),
        # Python-generated flags use fractions (0.35 = 35%).
        if abs(impact) > 1.0:
            impact /= 100.0

        if flag == "beneficiary" and trigger == "departed_team":
            total_adj += impact
        elif flag == "displaced" and trigger == "active_and_healthy":
            total_adj += impact
        elif flag == "scheme_fit":
            total_adj += impact * 0.5
        # committee → intentionally not processed here. Committee flags indicate
        # a timeshare between equals and have no direct valuation adjustment;
        # displaced (role lost to superior player) is the flag that carries impact.
        # contingent, injured/absent beneficiary → skip pre-draft

    if total_adj == 0.0:
        return ppr

    adjusted = ppr * (1.0 + total_adj)
    logger.info(
        "Dependency adjustment: %.1f → %.1f (%+.0f%%)",
        ppr, adjusted, total_adj * 100,
    )
    return max(adjusted, 0.0)


def _confidence(player: Player) -> str:
    """Infer data_confidence based on available profile data."""
    has_profile = player.profile is not None and player.profile.clean_season_baseline
    has_injury  = player.injury_profile is not None
    if has_profile and has_injury:
        return "high"
    if has_profile or has_injury:
        return "medium"
    return "low"
