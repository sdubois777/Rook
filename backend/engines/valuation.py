"""
Stage 9: Draft Bible Valuation Pass

Pure Python computation — no AI calls.

Synthesizes all pre-draft agent outputs (PlayerProfile, PlayerInjuryProfile)
into final valuation fields on the players table:
  - tier (1-5 per position)
  - baseline_value (PPR points → auction dollars via PAR method)
  - risk_adjusted_value (baseline × (1 + risk_modifier))
  - recommended_bid_ceiling (two-value formula from ARCHITECTURE.md)
  - let_go_threshold (bid ceiling × 1.15)
  - value_gap and value_gap_signal (system vs market gap)

Formulas from docs/ARCHITECTURE.md — Two-Value Auction System:

  Tier 1:
    blend = system_value × (1 - anchor_weight) + market_value × anchor_weight
    ceiling = blend × positional_scarcity_modifier × (1 + risk_modifier)

  Tier 2-3:
    blend = system_value × 0.85 + market_value × 0.15
    ceiling = blend × (1 + risk_modifier)

  Tier 4-5:
    ceiling = system_value × (1 + risk_modifier)

Anchor weights: T1=0.80, T2=0.40, T3=0.15, T4-5=0.00
Scarcity:       T1 RB=1.35, T1 WR=1.20, T1 QB/TE=1.10
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile
from backend.utils.seasons import get_analysis_year

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# League defaults
# ---------------------------------------------------------------------------

LEAGUE_BUDGET  = 200   # dollars per team — standard Yahoo auction
LEAGUE_TEAMS   = 12

# Budget share allocated to each position group (of total auction pool)
# Calibrated for 12-team, 1QB/2RB/3WR/1TE/1FLEX PPR auction format
POSITION_BUDGET_SHARE: dict[str, float] = {
    "QB": 0.12,
    "RB": 0.35,
    "WR": 0.38,
    "TE": 0.12,
}

# Replacement rank cutoff — lowest draftable starter at each position
REPLACEMENT_RANK: dict[str, int] = {
    "QB": 12,
    "RB": 30,
    "WR": 42,
    "TE": 12,
}

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
    1: Decimal("0.80"),
    2: Decimal("0.40"),
    3: Decimal("0.15"),
    4: Decimal("0.00"),
    5: Decimal("0.00"),
}

SCARCITY_MODIFIERS: dict[str, Decimal] = {
    "RB": Decimal("1.35"),
    "WR": Decimal("1.20"),
    "QB": Decimal("1.10"),
    "TE": Decimal("1.10"),
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
    risk_modifier: Optional[Decimal],
) -> Decimal:
    """
    Compute the recommended bid ceiling using the two-value formula.

    When market_value is None, treat market_value = system_value for blending
    (neutral blend — system value drives the result entirely).

    Returns:
        Decimal bid ceiling in dollars (minimum $1).
    """
    mv = market_value if market_value is not None else system_value
    rm = risk_modifier if risk_modifier is not None else Decimal("0")
    risk_factor = Decimal("1") + rm

    if tier == 1:
        anchor = ANCHOR_WEIGHTS[1]
        blend = system_value * (Decimal("1") - anchor) + mv * anchor
        scarcity = SCARCITY_MODIFIERS.get(position, Decimal("1.00"))
        ceiling = blend * scarcity * risk_factor

    elif tier in (2, 3):
        blend = system_value * Decimal("0.85") + mv * Decimal("0.15")
        ceiling = blend * risk_factor

    else:  # Tier 4-5
        ceiling = system_value * risk_factor

    return _to_dec(max(Decimal("1.00"), ceiling))


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


def compute_let_go_threshold(bid_ceiling: Decimal) -> Decimal:
    """Let-go threshold = bid ceiling + 15%."""
    return _to_dec(bid_ceiling * Decimal("1.15"))


def _to_dec(value: float | Decimal) -> Decimal:
    """Normalize to Decimal with 2dp."""
    return Decimal(str(float(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Async valuation pass — loads data, computes, writes back
# ---------------------------------------------------------------------------


async def run_valuation_pass(
    league_budget: int = LEAGUE_BUDGET,
    league_teams: int = LEAGUE_TEAMS,
) -> dict:
    """
    Load all players with profiles, compute valuations, write back to DB.

    Args:
        league_budget: Per-team auction budget (default 200).
        league_teams:  Number of teams in league (default 12).

    Returns:
        Summary dict: {processed, updated, skipped, analysis_year}.
    """
    analysis_year = get_analysis_year()
    total_budget = float(league_budget * league_teams)

    async with AsyncSessionLocal() as session:
        # Eager-load profiles and injury profiles — one query, no N+1
        stmt = (
            select(Player)
            .options(
                selectinload(Player.profile),
                selectinload(Player.injury_profile),
            )
        )
        players: list[Player] = (await session.execute(stmt)).scalars().all()

        # --------------- Group by position, extract ppr_points ---------------
        pos_groups: dict[str, list[tuple[Player, float]]] = {
            p: [] for p in DRAFTABLE_POSITIONS
        }
        for player in players:
            pos = player.position
            if pos not in DRAFTABLE_POSITIONS:
                continue
            ppr = _extract_ppr(player.profile)
            if ppr > 0:
                pos_groups[pos].append((player, ppr))

        # Sort each group descending by PPR
        for pos in pos_groups:
            pos_groups[pos].sort(key=lambda x: x[1], reverse=True)

        # --------------- Compute replacement levels + PAR per position -------
        par_context: dict[str, dict] = {}
        for pos, group in pos_groups.items():
            repl_rank = REPLACEMENT_RANK[pos]
            if len(group) >= repl_rank:
                repl_ppr = group[repl_rank - 1][1]
            else:
                repl_ppr = group[-1][1] if group else 0.0

            total_par = sum(max(0.0, ppr - repl_ppr) for _, ppr in group)
            pos_budget = total_budget * POSITION_BUDGET_SHARE[pos]

            par_context[pos] = {
                "replacement_ppr": repl_ppr,
                "total_par":       total_par,
                "position_budget": pos_budget,
            }

        # --------------- Compute and write valuations ------------------------
        processed = 0
        updated   = 0
        skipped   = 0

        for pos, group in pos_groups.items():
            ctx = par_context[pos]
            for rank_0, (player, ppr) in enumerate(group):
                rank = rank_0 + 1
                tier = assign_tier(rank)

                sv = ppr_to_system_value(
                    ppr_points        = ppr,
                    replacement_ppr   = ctx["replacement_ppr"],
                    total_par         = ctx["total_par"],
                    position_budget   = ctx["position_budget"],
                )

                rm = _get_risk_modifier(player.injury_profile)

                ceiling  = compute_bid_ceiling(sv, player.market_value, tier, pos, rm)
                let_go   = compute_let_go_threshold(ceiling)
                gap, sig = compute_value_gap(sv, player.market_value)
                risk_adj = _to_dec(sv * (Decimal("1") + (rm or Decimal("0"))))
                anchor   = ANCHOR_WEIGHTS.get(tier, Decimal("0.00"))
                scarcity = SCARCITY_MODIFIERS.get(pos, Decimal("1.00")) if tier == 1 else Decimal("1.00")

                # Update in-session player object
                player.tier                       = tier
                player.baseline_value             = sv
                player.risk_adjusted_value        = _to_dec(max(Decimal("1.00"), risk_adj))
                player.recommended_bid_ceiling    = ceiling
                player.let_go_threshold           = let_go
                player.elite_anchor_weight        = anchor
                player.positional_scarcity_modifier = scarcity
                player.value_gap                  = gap
                player.value_gap_signal           = sig
                player.data_confidence            = _confidence(player)

                session.add(player)
                processed += 1
                updated   += 1

        # Players with no profile (no ppr_points) — skip, count
        for player in players:
            if player.position in DRAFTABLE_POSITIONS:
                ppr = _extract_ppr(player.profile)
                if ppr <= 0:
                    skipped += 1

        await session.commit()

    logger.info(
        "Valuation pass (%d): %d updated, %d skipped, analysis_year=%d",
        processed, updated, skipped, analysis_year,
    )
    return {
        "processed":     processed,
        "updated":       updated,
        "skipped":       skipped,
        "analysis_year": analysis_year,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_ppr(profile: Optional[PlayerProfile]) -> float:
    """Extract ppr_points from clean_season_baseline JSONB, or 0."""
    if not profile or not profile.clean_season_baseline:
        return 0.0
    val = profile.clean_season_baseline.get("ppr_points", 0)
    try:
        return max(0.0, float(val or 0))
    except (TypeError, ValueError):
        return 0.0


def _get_risk_modifier(injury_profile: Optional[PlayerInjuryProfile]) -> Optional[Decimal]:
    """Return risk_adjusted_value_modifier from injury profile, or None."""
    if not injury_profile or injury_profile.risk_adjusted_value_modifier is None:
        return None
    return Decimal(str(injury_profile.risk_adjusted_value_modifier))


def _confidence(player: Player) -> str:
    """Infer data_confidence based on available profile data."""
    has_profile = player.profile is not None and player.profile.clean_season_baseline
    has_injury  = player.injury_profile is not None
    if has_profile and has_injury:
        return "high"
    if has_profile or has_injury:
        return "medium"
    return "low"
