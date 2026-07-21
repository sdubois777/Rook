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

Anchor weights (market weight per tier — system-dominant):
  T1=0.30, T2=0.45, T3=0.55, T4=0.70, T5=0.80
Scarcity:       T1 RB=1.35, T1 WR=1.20, T1 QB/TE=1.10
Risk discounts:  low=0%, moderate=8%, high=15%, volatile=22%
Let-go:          low=1.20×, moderate=1.15×, high=1.10×, volatile=1.05×
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile
from backend.models.league_config import LeagueConfig, DEFAULT_LEAGUE_CONFIG
from backend.utils.seasons import get_analysis_year, get_current_season

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

# Positions whose per-format budget shifts. QB is FORMAT-INVARIANT (QBs don't catch
# passes → identical points in every scoring format), like K/DEF — its budget share
# never moves. Only RB/WR/TE reprice on receptions, so only they reallocate.
_RECEPTION_POSITIONS: tuple[str, ...] = ("RB", "WR", "TE")


def _format_budget_shares(
    scoring_format: str,
    ppr_total_par: dict[str, float],
    fmt_total_par: dict[str, float],
) -> dict[str, float]:
    """Per-position auction-budget shares, made format-aware.

    PPR anchors on POSITION_BUDGET_SHARE verbatim (byte-identical to the players-table
    pass). For non-PPR, the RB/WR/TE pool reallocates: each reception position's PPR
    share is scaled by how much its aggregate value (total PAR) changed vs PPR, then
    renormalized back to that pool's combined share. So in Standard the WR pool's
    shrunken value hands budget to RB, instead of a fixed share re-inflating a
    compressed WR pool ($-up-while-tier-down at the *position* level). QB (and, by
    omission, K/DEF on their separate static path) never move.

    NOTE (ledger): this fixes only the CROSS-POSITION cause. Under Standard compression
    an elite pass-catcher's PAR *share within* its (shrunken) position can still rise
    faster than the budget cut, so a tier-falling player's non-PPR $ can exceed its PPR
    $. Auction $ must not go live on the Phase 2 surface until that within-position
    divergence is resolved. See run notes.
    """
    if scoring_format == "ppr":
        return dict(POSITION_BUDGET_SHARE)

    shares: dict[str, float] = {"QB": POSITION_BUDGET_SHARE["QB"]}
    raw: dict[str, float] = {}
    for pos in _RECEPTION_POSITIONS:
        base_par = ppr_total_par.get(pos) or 0.0
        fmt_par = fmt_total_par.get(pos) or 0.0
        raw[pos] = (
            POSITION_BUDGET_SHARE[pos] * (fmt_par / base_par)
            if base_par > 0
            else POSITION_BUDGET_SHARE[pos]
        )
    total_raw = sum(raw.values()) or 1.0
    target = sum(POSITION_BUDGET_SHARE[p] for p in _RECEPTION_POSITIONS)
    for pos in _RECEPTION_POSITIONS:
        shares[pos] = raw[pos] / total_raw * target
    return shares

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
    "QB": 17.0,
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
# Tier assignment — PAR-ratio-based (no rank caps)
# ---------------------------------------------------------------------------

# ═══════════════════════════════════════════════════════════════════════════
# TIERING — DISTRIBUTION-RELATIVE (z-score above the draftable-pool mean).
#
# A player's tier = how many standard deviations his projected points sit above
# the MEAN of the DRAFTABLE POOL at his position (top-K by points, K from the
# league config — NOT a constant). This is format-aware BY CONSTRUCTION: the
# format-specific points produce a format-specific mean/sigma, so the SAME z-cuts
# work for every position and every format — zero per-position, zero per-format
# constants. It self-calibrates to the pool, so it adapts to a cliff year (few
# elites) vs a bunched year (many) and survives replacement/projection drift.
#
# z-cuts justified by historical validation (three seasons of actuals), not chosen:
#   * z>=1.25 is more year-over-year STABLE than z>=1.0 (RB/WR T1 spread 0 vs 1-3)
#   * z>=1.25 lands in a SPARSER boundary region (fewer near-cut players → less
#     drift-flip). Both checks point the same way. See the tier-method recon.
# ═══════════════════════════════════════════════════════════════════════════
_Z_TIER_CUTS = {1: 1.25, 2: 0.4, 3: -0.2, 4: -0.7}  # tier N if z >= cut[N], else next
_Z_MIN_POOL = 5   # below this a pool has no meaningful sigma → absolute-threshold fallback

# FALLBACK ONLY — the legacy hardcoded absolute thresholds. These are NOT the live
# tiering path; they are used ONLY when a positional pool has < _Z_MIN_POOL players
# (no meaningful sigma), and using them emits a LOUD warning. Do not read these as the
# active thresholds — the live path is z-score (assign_tier_z / compute_pool_ztiers).
_FALLBACK_PAR_RATIO_THRESHOLDS = {
    "QB": {"T1": Decimal("1.15"), "T2": Decimal("1.03"), "T3": Decimal("0.95")},
    "RB": {"T1": Decimal("1.9"),  "T2": Decimal("1.5"),  "T3": Decimal("1.2")},
    "WR": {"T1": Decimal("2.0"),  "T2": Decimal("1.5"),  "T3": Decimal("1.2")},
    "TE": {"T1": Decimal("1.755"), "T2": Decimal("1.5"),  "T3": Decimal("1.2")},
}

_T4_FLOOR = Decimal("0.8")  # T4 fallback floor: >= 0.8x replacement, all positions


def assign_tier(par_ratio: float, position: str) -> int:
    """FALLBACK tiering — absolute PAR-ratio thresholds. Used only when a positional pool
    is too small for a distribution-relative tier (< _Z_MIN_POOL). The LIVE tiering path is
    compute_pool_ztiers() below. Kept format-BLIND on purpose (it is a last resort)."""
    ratio = Decimal(str(par_ratio))
    thresholds = _FALLBACK_PAR_RATIO_THRESHOLDS.get(position, _FALLBACK_PAR_RATIO_THRESHOLDS["WR"])
    if ratio >= thresholds["T1"]:
        return 1
    if ratio >= thresholds["T2"]:
        return 2
    if ratio >= thresholds["T3"]:
        return 3
    if ratio >= _T4_FLOOR:
        return 4
    return 5


def z_to_tier(z: float) -> int:
    """Map a within-pool z-score to a 1-5 tier via the shared (position- and format-
    agnostic) z-cuts. Higher z = higher tier."""
    if z >= _Z_TIER_CUTS[1]:
        return 1
    if z >= _Z_TIER_CUTS[2]:
        return 2
    if z >= _Z_TIER_CUTS[3]:
        return 3
    if z >= _Z_TIER_CUTS[4]:
        return 4
    return 5


def compute_pool_ztiers(
    ranked_points: list[float], pool_size: int, position: str,
) -> tuple[Optional[list[int]], Optional[float], Optional[float]]:
    """LIVE tiering. Given points sorted DESC for one position, tier every player by
    z-score over the DRAFTABLE POOL (top `pool_size` — the $1 depth tail is excluded so it
    can't drag the mean / inflate sigma). Returns (tiers_for_all_players, mean, sigma):
      * tiers list aligns 1:1 with ranked_points (players beyond the pool tier off the same
        pool mean/sigma → naturally T4/T5).
      * Returns (None, None, None) when the pool is too small (< _Z_MIN_POOL) or sigma==0 —
        the caller then falls back to assign_tier() with a LOUD warning.
    """
    pool = [p for p in ranked_points if p and p > 0][:pool_size]
    if len(pool) < _Z_MIN_POOL:
        return None, None, None
    mu = sum(pool) / len(pool)
    var = sum((p - mu) ** 2 for p in pool) / len(pool)
    sigma = var ** 0.5
    if sigma == 0:
        return None, None, None
    tiers = [z_to_tier((p - mu) / sigma) for p in ranked_points]
    return tiers, mu, sigma


# ---------------------------------------------------------------------------
# Anchor weights and scarcity modifiers
# ---------------------------------------------------------------------------

ANCHOR_WEIGHTS: dict[int, Decimal] = {
    1: Decimal("0.30"),
    2: Decimal("0.45"),
    3: Decimal("0.55"),
    4: Decimal("0.70"),
    5: Decimal("0.80"),
}

# ---------------------------------------------------------------------------
# Tier-band auction pricing (per-format Half/Standard ONLY — see write_format_value_sets)
# ---------------------------------------------------------------------------
# The legacy pool-share auction-$ (par/total_par × budget) inverts under Standard
# compression: as fewer players clear replacement the position pool shrinks, so an
# elite pass-catcher's SHARE rises even though his value-over-replacement falls — a
# tier-falling player gets MORE dollars. Tier-band pricing instead derives $ from the
# per-format TIER (which already moves correctly), so a player who tier-falls prices
# down. Each (position, tier) gets a dollar band from these multipliers, a within-tier
# par gradient spreads players inside a band, then ONE global rescale per format hits
# the skill budget — GLOBAL (not per-position) so when Standard collapses WR tiers the
# freed pool flows to RB (rushers rise), the market-correct behavior. Validated by
# experiment (9/10 direction vs market, pool-sum preserved, no market-data dependency).
#
# QB / K / DEF are FORMAT-INVARIANT (no receptions → identical points every format) so
# they are NOT tier-banded — they keep their existing pool-share value, which is already
# identical across formats. Only the reception positions reprice.
TIER_BAND_MULTIPLIERS: dict[int, float] = {1: 1.0, 2: 0.50, 3: 0.24, 4: 0.10, 5: 0.02}
TIER_BAND_GRADIENT = 0.28  # ± within-tier spread by par rank (so a tier doesn't price flat)
_TIER_BAND_POSITIONS: tuple[str, ...] = ("RB", "WR", "TE")


def _compute_tier_band_sv(
    par_ctx: dict[str, tuple],
    ppr_tier_mass: dict[str, float],
    total_budget: float,
    ztier_by_pos: Optional[dict[str, dict]] = None,
) -> dict:
    """Tier-derived auction-$ for the reception positions in ONE format.

    par_ctx[pos] = (group, repl_ppr, total_par) where group = [(player, raw_ppr, adj)].
    ppr_tier_mass[pos] = Σ TIER_BAND_MULTIPLIERS[tier] over that position's PPR tiers —
    the PPR anchor that pins each position's share at its budget target in PPR, so any
    non-PPR shift is driven purely by tier movement. Returns {player.id: Decimal $}.

    ztier_by_pos: the LIVE distribution-relative tiers {pos: {player.id: tier}}. When a
    player is absent (tiny pool) it falls back to the absolute assign_tier.
    """
    from collections import defaultdict

    ztier_by_pos = ztier_by_pos or {}
    skill_budget = total_budget * sum(POSITION_BUDGET_SHARE[p] for p in _TIER_BAND_POSITIONS)
    tier_groups: dict[tuple, list] = defaultdict(list)
    for pos in _TIER_BAND_POSITIONS:
        if pos not in par_ctx:
            continue
        group, repl_ppr, _ = par_ctx[pos]
        _tmap = ztier_by_pos.get(pos, {})
        for player, raw_ppr, adj in group:
            par_ratio = raw_ppr / repl_ppr if repl_ppr > 0 else 0.0
            tier = _tmap.get(player.id) or assign_tier(par_ratio, pos)
            # Rank the within-tier gradient by the SAME raw points the tier is built from
            # (not the injury/dependency-adjusted points) so a higher-projected player is
            # never priced below a lower one in the same tier — a strict rank index also
            # keeps ties from collapsing to one z, so ordering is monotonic by projection.
            tier_groups[(pos, tier)].append((player.id, raw_ppr))

    raw: dict = {}
    for (pos, tier), members in tier_groups.items():
        mass = ppr_tier_mass.get(pos) or 1.0
        scale = total_budget * POSITION_BUDGET_SHARE[pos] / mass
        order = sorted(members, key=lambda m: m[1], reverse=True)
        rank = {pid: i for i, (pid, _) in enumerate(order)}
        n = len(order)
        for pid, _pts in members:
            z = (1.0 - 2.0 * rank[pid] / (n - 1)) if n > 1 else 0.0
            raw[pid] = max(1.0, scale * TIER_BAND_MULTIPLIERS[tier] * (1 + TIER_BAND_GRADIENT * z))

    total_raw = sum(raw.values()) or 1.0
    k = skill_budget / total_raw
    return {pid: _to_dec(max(1.0, v * k)) for pid, v in raw.items()}

SCARCITY_MODIFIERS: dict[str, Decimal] = {
    "RB": Decimal("1.35"),
    "WR": Decimal("1.20"),
    "QB": Decimal("1.10"),
    "TE": Decimal("1.10"),
}

# ---------------------------------------------------------------------------
# K / DEF — separate STATIC streaming valuation (NOT the skill pipeline)
# ---------------------------------------------------------------------------
# K and DEF have no profile, no usage/snap data, and no positional anchors, so
# the PAR / scarcity / trajectory machinery is undefined or wrong-scale for them
# and would produce garbage. They are $1 streamers: assign a flat, STATIC value
# and write the SAME shared output fields skill players use, so the draft/trade
# surfaces (T4) read them with zero position awareness. Within-position ordering
# is intentionally FLAT for launch (every K identical, every DEF identical) — see
# the FantasyPros hook in value_kdef().
_KDEF_POSITIONS = frozenset({"K", "DEF"})
# Positions copied verbatim from the players table into every format's PFV row (no
# per-format reprice): they score identically in all formats, so their auction $ is
# format-invariant. QB joins K/DEF here — QBs don't catch passes.
_FORMAT_INVARIANT_POSITIONS = frozenset({"QB", "K", "DEF"})
_KDEF_TIER = 5          # assign_tier's floor (streamer) tier
_KDEF_BASE_BID = 1      # $1 base — clamped to MAX_REALISTIC_BID ($2) below


def value_kdef(player: Player) -> None:
    """Static streaming valuation for one K or DEF.

    Writes the shared output fields DIRECTLY — no projection→PAR→value chain (K/DEF
    have no clean_season_baseline). Deliberately position-agnostic on output so the
    rejoin is complete: tier, baseline/ceiling/floor value, risk-adjusted value,
    recommended + ai bid ceiling (clamped to the $2 K/DEF cap), and adp_ai (adp_rank
    is then assigned globally by valuation_agent.assign_adp_ranks, which ranks any
    player with a non-null adp_ai). Idempotent.
    """
    # Lazy import: the ADP ranges live in valuation_agent; import at call time so
    # module import order can't create a cycle.
    from backend.agents.valuation_agent import ADP_POSITION_RANGES

    pos = player.position
    bid = min(_KDEF_BASE_BID, MAX_REALISTIC_BID.get(pos, _KDEF_BASE_BID))  # $1, cap $2
    bid_dec = Decimal(str(bid))

    # adp_ai = the START of the position's clamp range (DEF=130 / K=140) — places
    # them in the final rounds. FLAT: every DEF shares 130, every K 140, so
    # within-position ordering is a tie for launch.
    #
    # >>> FANTASYPROS K/DEF ADP HOOK <<<
    # To give real per-defense / per-kicker ordering, look up a scraped FantasyPros
    # K/DEF ADP for this player here and use it in place of `range_start`. Building
    # that scrape is an optional later enhancement — OUT OF SCOPE for T1.
    range_start = ADP_POSITION_RANGES.get(pos, (200, 200))[0]
    adp = Decimal(str(range_start))

    player.tier                         = _KDEF_TIER
    player.baseline_value               = bid_dec
    player.ceiling_value                = bid_dec
    player.floor_value                  = bid_dec
    player.risk_adjusted_value          = bid_dec
    player.recommended_bid_ceiling      = bid_dec
    player.let_go_threshold             = bid_dec
    player.ai_bid_ceiling               = bid            # auction surface (int)
    player.adp_ai                       = adp            # snake surface
    player.elite_anchor_weight          = ANCHOR_WEIGHTS.get(_KDEF_TIER, Decimal("0.00"))
    player.positional_scarcity_modifier = Decimal("1.00")
    player.value_gap                    = None           # no market comparison for $1 streamers
    player.value_gap_signal             = "aligned"
    player.data_confidence              = "low"


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
    effective = fp if (fp is not None and fp > 0) else league

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

    Market source: market_value_fantasypros (consensus ADP, shared across all users).
    """
    market = getattr(player, "market_value_fantasypros", None)
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
        "QB": qb_starters + 1,  # 1-QB league: replacement = first non-starter
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
# Shared per-player value math — the SINGLE computation used by BOTH the PPR pass
# (run_valuation_pass, writing the players table) and the per-format writer
# (write_format_value_sets, writing player_format_values). Keeping it in one place
# is what prevents PPR and the per-format rows from drifting.
# ---------------------------------------------------------------------------
def _value_fields_for(
    player, raw_ppr: float, adjusted_ppr: float,
    repl_ppr, total_par: float, pos_budget: float, pos: str,
    upside_ppr: float, downside_ppr: float,
    override_sv: Optional[Decimal] = None,
    tier_override: Optional[int] = None,
) -> dict:
    """Compute the full value-field set for one player from its (already
    format-repriced) points + the position's PAR context. Pure — no writes.

    tier_override: the distribution-relative (z-score) tier computed over the positional
    pool by compute_pool_ztiers(). This is the LIVE tier. Only when it is None (pool too
    small for a meaningful sigma) does this fall back to assign_tier() with a loud warning.

    override_sv (Half/Standard reception positions): the tier-band auction-$ replaces
    the legacy pool-share system_value. When set, ALL dollar fields derive from it (the
    ceiling anchors on the tier-band $ with NO PPR market blend, so non-PPR $ stay on
    the per-format basis; ceiling/floor scale by the upside/downside points ratio). PPR
    and format-invariant positions (override_sv=None) keep the exact pool-share path.
    """
    par_ratio = raw_ppr / repl_ppr if repl_ppr > 0 else 0.0
    if tier_override is not None:
        tier = tier_override
    else:
        tier = assign_tier(par_ratio, pos)
        logger.warning(
            "TIER FALLBACK (absolute thresholds, NOT z-score) used for %s (%s) — "
            "pool too small for a distribution-relative tier",
            getattr(player, "name", "?"), pos,
        )
    sv = override_sv if override_sv is not None else ppr_to_system_value(
        adjusted_ppr, repl_ppr, total_par, pos_budget)

    risk_level = "low"
    if player.injury_profile and player.injury_profile.overall_risk_level:
        risk_level = player.injury_profile.overall_risk_level
    rm = _get_risk_modifier(player.injury_profile)

    # Non-PPR tier-band $ anchor purely on the tier-band value (market=None) so the PPR
    # ADP market value never leaks into a per-format ceiling; PPR keeps the market blend.
    ceiling_mv = None if override_sv is not None else get_market_context(player)["effective_market_value"]
    ceiling = compute_bid_ceiling(sv, ceiling_mv, tier, pos, risk_level)
    max_bid_dec = Decimal(str(MAX_REALISTIC_BID.get(pos, 80)))
    if ceiling > max_bid_dec:
        ceiling = max_bid_dec

    let_go = compute_let_go_threshold(ceiling, risk_level)
    risk_adj = _to_dec(sv * (Decimal("1") + (rm or Decimal("0"))))
    anchor = ANCHOR_WEIGHTS.get(tier, Decimal("0.00"))
    scarcity = SCARCITY_MODIFIERS.get(pos, Decimal("1.00")) if tier == 1 else Decimal("1.00")

    if override_sv is not None:
        # Scale ceiling/floor $ by the upside/downside points ratio to the projection
        # (the pool-share ppr_to_system_value basis no longer applies to these rows).
        base_pts = adjusted_ppr if adjusted_ppr > 0 else raw_ppr
        ceiling_val = _to_dec(sv * Decimal(str(upside_ppr / base_pts))) if (upside_ppr > 0 and base_pts > 0) else None
        floor_val = _to_dec(sv * Decimal(str(downside_ppr / base_pts))) if (downside_ppr > 0 and base_pts > 0) else None
    else:
        ceiling_val = ppr_to_system_value(upside_ppr, repl_ppr, total_par, pos_budget) if upside_ppr > 0 else None
        floor_val = ppr_to_system_value(downside_ppr, repl_ppr, total_par, pos_budget) if downside_ppr > 0 else None

    return {
        "tier": tier,
        "baseline_value": sv,
        "ceiling_value": ceiling_val,
        "floor_value": floor_val,
        "risk_adjusted_value": _to_dec(max(Decimal("1.00"), risk_adj)),
        "recommended_bid_ceiling": ceiling,
        "let_go_threshold": let_go,
        "elite_anchor_weight": anchor,
        "positional_scarcity_modifier": scarcity,
        "replacement_ppr": repl_ppr,
    }


# ---------------------------------------------------------------------------
# Async valuation pass — loads data, computes, writes back
# ---------------------------------------------------------------------------


async def run_valuation_pass(
    config: LeagueConfig = DEFAULT_LEAGUE_CONFIG,
    dry_run: bool = False,
    prior_production: Optional[dict] = None,
) -> dict:
    """
    Load all players with profiles, compute valuations, write back to DB.

    dry_run=True computes everything but writes NOTHING (rolls back the session)
    and returns a per-player before/after report in result["report"] — used to
    review a math change's effect before committing it to prod.

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

        # STEP 4 — displaced direction guard. prior_production ({key: (ppg, games)}) is
        # passed in by the pipeline (pure data load, no AI); when absent the guard is inert
        # so existing callers/tests are unaffected. suppressions are collected for the report.
        displaced_suppressed: list[dict] = []

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
                adjusted_ppr = _apply_dependency_adjustment(
                    adjusted_ppr, player.dependencies,
                    player_name=player.name,
                    prior_production=prior_production,
                    suppressed_log=displaced_suppressed,
                )
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
            # Use adjusted_ppr (x[2]) for PAR calculations — dollar values reflect risk.
            # `group` is sorted by RAW ppr, but calculate_replacement_level indexes its
            # input as descending-SORTED, so sort the adjusted values here — otherwise a
            # discounted near-cutoff player (e.g. an injured TE) drags replacement below
            # the true marginal value and inflates every par-ratio above it.
            sorted_pprs = sorted((adj_ppr for _, _, adj_ppr in group), reverse=True)
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

            # Distribution-relative tiers over this position's DRAFTABLE POOL. Uses RAW ppr
            # (group is raw-sorted) — tier is talent/role, never risk. Keyed by player.id.
            raw_ranked = [rp for _, rp, _ in group]  # already sorted desc by raw ppr
            ztiers, _zmu, _zsd = compute_pool_ztiers(raw_ranked, pool_size, pos)
            tier_by_id = ({group[i][0].id: ztiers[i] for i in range(len(group))}
                          if ztiers is not None else {})

            par_context[pos] = {
                "replacement_ppr": repl_ppr,
                "total_par":       total_par,
                "position_budget": pos_budget,
                "pool_size":       pool_size,
                "tier_by_id":      tier_by_id,
            }

            logger.info(
                "PAR context %s: pool=%d, repl=%.1f PPR (#%d of %d players), "
                "total_par=%.1f, budget=$%.0f, z-tiers=%s (mu=%.0f sd=%.0f)",
                pos, pool_size, repl_ppr,
                min(pool_size, len(group)), len(group),
                total_par, pos_budget,
                "yes" if ztiers is not None else "FALLBACK",
                _zmu or 0, _zsd or 0,
            )

        # --------------- Compute and write valuations ------------------------
        processed = 0
        updated   = 0
        skipped   = 0
        dry_report: list[dict] = []

        # K/DEF take the SEPARATE static streaming path — they NEVER enter the
        # skill PAR/scarcity machinery above (pos_groups holds only DRAFTABLE_POSITIONS,
        # so K/DEF were skipped at the grouping gate). Value them here, writing the
        # shared output fields so the rejoin is position-agnostic. FA K/DEF (no team)
        # are skipped; T2 ingestion already excludes them, this is belt-and-suspenders.
        for player in players:
            if (
                player.position in _KDEF_POSITIONS
                and player.team_abbr
                and player.team_abbr != "FA"
            ):
                value_kdef(player)
                session.add(player)
                valued_player_ids.add(player.id)
                processed += 1
                updated   += 1

        for pos, group in pos_groups.items():
            ctx = par_context[pos]
            for player, raw_ppr, adjusted_ppr in group:
                # Compute ceiling/floor dollar values from upside/downside PPR
                upside_ppr, downside_ppr = _extract_upside_downside(player.profile)
                vf = _value_fields_for(
                    player, raw_ppr, adjusted_ppr,
                    ctx["replacement_ppr"], ctx["total_par"], ctx["position_budget"], pos,
                    upside_ppr, downside_ppr,
                    tier_override=ctx["tier_by_id"].get(player.id),
                )
                # Capture before/after for the dry-run diff (old = current DB row).
                _new_par = (raw_ppr / ctx["replacement_ppr"]) if ctx["replacement_ppr"] else 0.0
                dry_report.append({
                    "name":     player.name,
                    "pos":      pos,
                    "old_tier": player.tier,
                    "new_tier": vf["tier"],
                    "old_base": float(player.baseline_value) if player.baseline_value is not None else None,
                    "new_base": float(vf["baseline_value"]) if vf["baseline_value"] is not None else None,
                    "par":      round(_new_par, 3),
                    "raw_ppr":  round(raw_ppr, 1),
                })
                # Update in-session player object — set values BEFORE gap
                # so compute_value_gap_from_player sees current ceiling
                player.tier                       = vf["tier"]
                player.baseline_value             = vf["baseline_value"]
                player.ceiling_value              = vf["ceiling_value"]
                player.floor_value                = vf["floor_value"]
                player.risk_adjusted_value        = vf["risk_adjusted_value"]
                player.recommended_bid_ceiling    = vf["recommended_bid_ceiling"]
                player.let_go_threshold           = vf["let_go_threshold"]
                player.elite_anchor_weight        = vf["elite_anchor_weight"]
                player.positional_scarcity_modifier = vf["positional_scarcity_modifier"]

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

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    logger.info(
        "Valuation pass (%d): %d updated, %d skipped, %d cleared, analysis_year=%d (dry_run=%s)",
        processed, updated, skipped, cleared, analysis_year, dry_run,
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
        "dry_run":       dry_run,
        "report":        dry_report,
        "displaced_suppressed": displaced_suppressed,
    }


# STEP 5 — margin (in $) below market beyond which a "PAY UP" badge is contradictory.
PAY_UP_SUPPRESS_MARGIN = Decimal("5")


async def reconcile_value_signals(dry_run: bool = False) -> dict:
    """Recompute value_gap / value_gap_signal AFTER the valuation agent has written the
    final ai_bid_ceiling (Phase 6), and reconcile pay_up_flag. Pure DB pass — NO AI calls.

    Fixes the ordering bug: run_valuation_pass (Phase 5) computes value_gap from the PRIOR
    run's ai_bid_ceiling (Phase 6 overwrites it afterward), so every displayed gap is one
    cycle stale. This recomputes against the current ceiling. It also suppresses pay_up_flag
    when our ceiling sits more than PAY_UP_SUPPRESS_MARGIN below market — so "PAY UP at $44
    vs $61" can't render. Uses market_value_fantasypros (same basis as compute_value_gap_from_
    player) — the client chip's cheap/small-gap guards read league price separately; this does
    not change that, it only makes the stored gap fresh and the pay_up badge non-contradictory.
    """
    updated = 0
    payup_suppressed: list[dict] = []
    report: list[dict] = []
    async with AsyncSessionLocal() as session:
        players = (await session.execute(
            select(Player).where(Player.ai_bid_ceiling.isnot(None))
        )).scalars().all()
        for p in players:
            old_gap = float(p.value_gap) if p.value_gap is not None else None
            old_sig = p.value_gap_signal
            old_payup = bool(p.pay_up_flag)

            gap, sig = compute_value_gap_from_player(p)

            new_payup = old_payup
            market = getattr(p, "market_value_fantasypros", None)
            ceil = getattr(p, "ai_bid_ceiling", None)
            if (
                old_payup and market and ceil
                and _to_dec(ceil) < _to_dec(market) - PAY_UP_SUPPRESS_MARGIN
            ):
                new_payup = False
                payup_suppressed.append({
                    "player": p.name,
                    "ai_bid_ceiling": float(ceil),
                    "market_fp": float(market),
                })

            new_gap = float(gap) if gap is not None else None
            if dry_run:
                if old_gap != new_gap or old_sig != sig or old_payup != new_payup:
                    report.append({
                        "name": p.name,
                        "gap": [old_gap, new_gap],
                        "signal": [old_sig, sig],
                        "pay_up": [old_payup, new_payup],
                    })
            else:
                p.value_gap = gap
                p.value_gap_signal = sig
                p.pay_up_flag = new_payup
                session.add(p)
                updated += 1

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    return {
        "updated": updated,
        "payup_suppressed": payup_suppressed,
        "report": report,
        "dry_run": dry_run,
    }


async def write_format_value_sets(
    config: LeagueConfig = DEFAULT_LEAGUE_CONFIG,
    prior_production: Optional[dict] = None,
    dry_run: bool = False,
) -> dict:
    """Reprice the board into ALL scoring formats and write player_format_values.

    Runs AFTER run_valuation_pass (which populates the authoritative PPR values on the
    players table). For each format it reprices points via _extract_ppr(profile, fmt)
    and reuses the SAME shared math (_value_fields_for + the identical PAR context) —
    so the PPR rows equal the players-table values (asserted in tests) and Half/Standard
    differ only by the reception delta. K/DEF are format-invariant: their rows copy the
    players-table values. Upsert on (player_id, scoring_format) — re-runnable.
    """
    from sqlalchemy import func as _func
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from backend.models.player_format_values import PlayerFormatValues
    from backend.scoring import SCORING_FORMATS

    total_budget = config.total_skill_pool
    league_teams = config.team_count
    pool_sizes = get_draftable_pool_sizes(league_teams)
    written = 0
    dry_rows: list[dict] = []
    suppressed: list[dict] = []  # STEP 4 guard firings (same players each format)

    async with AsyncSessionLocal() as session:
        players = (await session.execute(
            select(Player).options(
                selectinload(Player.profile),
                selectinload(Player.injury_profile),
                selectinload(Player.dependencies),
            )
        )).scalars().all()

        async def _upsert(row: dict) -> None:
            nonlocal written
            if dry_run:
                dry_rows.append(row)
                written += 1
                return
            stmt = pg_insert(PlayerFormatValues).values(**row)
            update = {k: row[k] for k in row if k not in ("player_id", "scoring_format")}
            update["updated_at"] = _func.now()
            await session.execute(stmt.on_conflict_do_update(
                constraint="uq_player_format", set_=update))
            written += 1

        # PPR's per-position total PAR anchors the format-aware budget shift below.
        # SCORING_FORMATS puts "ppr" first, so this is populated before it's read.
        ppr_total_par: dict[str, float] = {}
        # PPR tier mass per reception position anchors tier-band pricing (below), also
        # populated on the ppr pass before any non-PPR pass reads it.
        ppr_tier_mass: dict[str, float] = {}

        for fmt in SCORING_FORMATS:
            # Group skill positions with per-format points (identical shape to the PPR pass).
            pos_groups: dict[str, list] = {p: [] for p in DRAFTABLE_POSITIONS}
            for player in players:
                pos = player.position
                if pos not in DRAFTABLE_POSITIONS or not player.team_abbr or player.team_abbr == "FA":
                    continue
                raw_ppr = _extract_ppr(player.profile, fmt)
                if raw_ppr <= 0:
                    continue
                adjusted_ppr = _apply_injury_discount(raw_ppr, player.injury_profile, player.profile)
                if adjusted_ppr > 0:
                    adjusted_ppr = _apply_dependency_adjustment(
                        adjusted_ppr, player.dependencies,
                        player_name=player.name,
                        prior_production=prior_production,
                        # collect firings once (ppr pass) — the same players suppress every format
                        suppressed_log=suppressed if fmt == "ppr" else None,
                    )
                pos_groups[pos].append((player, raw_ppr, max(0.0, adjusted_ppr)))
            for pos in pos_groups:
                pos_groups[pos].sort(key=lambda x: x[1], reverse=True)

            # PASS 1 — per-position PAR context (replacement level + total PAR).
            # ztier_by_pos: distribution-relative (z-score) tiers over this FORMAT's pool,
            # keyed by player.id — the format-specific points give a format-specific mean/
            # sigma, so the same z-cuts yield format-appropriate tiers. Empty per-pos map
            # → the pool was too small (fallback to assign_tier in the consumers, loudly).
            par_ctx: dict[str, tuple] = {}
            ztier_by_pos: dict[str, dict] = {}
            for pos, group in pos_groups.items():
                pool_size = pool_sizes.get(pos, len(group))
                # `group` is RAW-sorted; calculate_replacement_level assumes descending-
                # sorted input, so sort the adjusted values (see the players-table pass).
                sorted_pprs = sorted((adj for _, _, adj in group), reverse=True)
                dynamic_repl = calculate_replacement_level(sorted_pprs, pool_size)
                floor_ppr = REPLACEMENT_LEVEL_PPR_PER_GAME.get(pos, 0.0) * 17
                max_ppr = REPLACEMENT_LEVEL_MAX_PPR_PER_GAME.get(pos, 15.0) * 17
                repl_ppr = min(max(dynamic_repl, floor_ppr), max_ppr)
                total_par = sum(max(0.0, adj - repl_ppr) for _, _, adj in group)
                par_ctx[pos] = (group, repl_ppr, total_par)
                # z-tiers on RAW ppr (group is raw-sorted) — tier is talent, not risk.
                ztiers, _mu, _sd = compute_pool_ztiers([rp for _, rp, _ in group], pool_size, pos)
                ztier_by_pos[pos] = ({group[i][0].id: ztiers[i] for i in range(len(group))}
                                     if ztiers is not None else {})

            # FORMAT-AWARE position budgets (see _format_budget_shares). PPR anchors on
            # the fixed shares (byte-identical); non-PPR shifts the reception-affected
            # RB/WR/TE pool by per-format PAR so a shrunken WR pool's budget flows to RB.
            fmt_total_par = {pos: par_ctx[pos][2] for pos in par_ctx}
            if fmt == "ppr":
                ppr_total_par = fmt_total_par
                # Capture PPR tier mass (Σ multipliers) per reception position — the
                # anchor for tier-band pricing of the non-PPR formats.
                for pos in _TIER_BAND_POSITIONS:
                    if pos in par_ctx:
                        group, repl_ppr, _ = par_ctx[pos]
                        _tmap = ztier_by_pos.get(pos, {})
                        ppr_tier_mass[pos] = sum(
                            TIER_BAND_MULTIPLIERS[_tmap.get(
                                pl.id, assign_tier(rp / repl_ppr if repl_ppr > 0 else 0.0, pos))]
                            for pl, rp, _ in group
                        )
            budget_share = _format_budget_shares(fmt, ppr_total_par, fmt_total_par)

            # Tier-band auction-$ for the reception positions — Half/Standard ONLY. PPR
            # keeps its exact pool-share baseline (byte-identical for current users); this
            # replaces the inverting pool-share $ with a tier-derived $ for non-PPR.
            tier_band_sv: dict = {}
            if fmt != "ppr":
                tier_band_sv = _compute_tier_band_sv(par_ctx, ppr_tier_mass, total_budget, ztier_by_pos)

            # PASS 2 — values, with the format-aware position budget. QB is FORMAT-
            # INVARIANT (no receptions) so it is copied from the players table below
            # (like K/DEF), never repriced per format — skip it in the skill loop.
            for pos, (group, repl_ppr, total_par) in par_ctx.items():
                if pos in _FORMAT_INVARIANT_POSITIONS:
                    continue
                pos_budget = total_budget * budget_share.get(pos, POSITION_BUDGET_SHARE.get(pos, 0.0))
                _tmap = ztier_by_pos.get(pos, {})
                for player, raw_ppr, adjusted_ppr in group:
                    up, down = _extract_upside_downside(player.profile, fmt)
                    vf = _value_fields_for(
                        player, raw_ppr, adjusted_ppr, repl_ppr, total_par, pos_budget, pos, up, down,
                        override_sv=tier_band_sv.get(player.id),
                        tier_override=_tmap.get(player.id))
                    await _upsert({
                        "player_id": player.id, "scoring_format": fmt,
                        "projected_points": round(raw_ppr, 1),
                        "replacement_ppr": round(repl_ppr, 1),
                        "tier": vf["tier"], "baseline_value": vf["baseline_value"],
                        "recommended_bid_ceiling": vf["recommended_bid_ceiling"],
                        "ceiling_value": vf["ceiling_value"], "floor_value": vf["floor_value"],
                        "risk_adjusted_value": vf["risk_adjusted_value"],
                    })

            # QB / K / DEF: format-invariant — copy the players-table (PPR) values
            # verbatim (identical points every format → identical auction $, so a QB's
            # non-PPR price equals its PPR price and can never drift from the pass).
            for player in players:
                if (player.position in _FORMAT_INVARIANT_POSITIONS and player.team_abbr
                        and player.team_abbr != "FA" and player.baseline_value is not None):
                    await _upsert({
                        "player_id": player.id, "scoring_format": fmt,
                        "projected_points": None, "replacement_ppr": None,
                        "tier": player.tier, "baseline_value": player.baseline_value,
                        "recommended_bid_ceiling": player.recommended_bid_ceiling,
                        "ceiling_value": player.ceiling_value, "floor_value": player.floor_value,
                        "risk_adjusted_value": player.risk_adjusted_value,
                    })

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    logger.info("Per-format value sets written: %d rows across %s (dry_run=%s)",
                written, list(SCORING_FORMATS), dry_run)
    return {"written": written, "formats": list(SCORING_FORMATS),
            "dry_run": dry_run, "report": dry_rows, "suppressed": suppressed}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _baseline_receptions(baseline: dict, *, projected: bool) -> float:
    """The reception count to reprice with. For a forward projection, use
    projected_receptions (falls back to the historical baseline receptions); for the
    historical baseline points, use the historical receptions. 0 when unknown → the
    reprice is a no-op (correct for non-receivers and the honest fallback)."""
    key = "projected_receptions" if projected else "receptions"
    val = baseline.get(key)
    if val is None and projected:
        val = baseline.get("receptions")   # projection without its own rec count
    try:
        return max(0.0, float(val or 0))
    except (TypeError, ValueError):
        return 0.0


def _extract_ppr(profile: Optional[PlayerProfile], scoring_format: str = "ppr") -> float:
    """Extract points from clean_season_baseline JSONB, repriced into `scoring_format`.

    Prefers projected_ppr_season (forward Sonnet projection) over ppr_points
    (historical baseline). The stored total is PPR; `scoring.season_points` backs out
    the reception delta for Half/Standard (exact — only receptions differ across
    presets). scoring_format="ppr" is the identity, so the PPR path is unchanged.
    """
    if not profile or not profile.clean_season_baseline:
        return 0.0
    baseline = profile.clean_season_baseline
    projected = baseline.get("projected_ppr_season") is not None
    val = baseline.get("projected_ppr_season") or baseline.get("ppr_points", 0)
    try:
        ppr_total = max(0.0, float(val or 0))
    except (TypeError, ValueError):
        return 0.0
    if scoring_format == "ppr":
        return ppr_total
    from backend import scoring
    return scoring.season_points(ppr_total, _baseline_receptions(baseline, projected=projected), scoring_format)


def _extract_upside_downside(
    profile: Optional[PlayerProfile], scoring_format: str = "ppr"
) -> tuple[float, float]:
    """Extract upside_ppr and downside_ppr, repriced into `scoring_format`, or (0, 0)."""
    if not profile or not profile.clean_season_baseline:
        return 0.0, 0.0
    baseline = profile.clean_season_baseline
    try:
        upside = max(0.0, float(baseline.get("upside_ppr", 0) or 0))
        downside = max(0.0, float(baseline.get("downside_ppr", 0) or 0))
    except (TypeError, ValueError):
        return 0.0, 0.0
    if scoring_format != "ppr" and (upside or downside):
        from backend import scoring
        rec = _baseline_receptions(baseline, projected=True)
        upside = scoring.season_points(upside, rec, scoring_format) if upside else 0.0
        downside = scoring.season_points(downside, rec, scoring_format) if downside else 0.0
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


# STEP 4 — displaced direction guard. A "displaced" flag says the flagged player
# lost role to a SUPERIOR trigger player. When the flagged player actually OUT-PRODUCED
# the trigger last season (per-game, both with a real sample), the direction is backwards
# and the negative $ adjustment is wrong (Puka 375 flagged displaced by Adams 223). We
# suppress the negative adjustment. Per-game + a games floor on BOTH players so a trigger
# who merely MISSED TIME (injured, e.g. Kyren Williams 0.5 PPR / ~1 game) does not trip the
# guard — that displaced flag (Corum) is legitimate and must stand.
_DISPLACED_GUARD_MIN_GAMES = 8


def _prod_key_full(name: str) -> str:
    """'Puka Nacua' -> 'pnacua' (first initial + surname, punctuation-stripped)."""
    parts = re.sub(r"[.']", "", (name or "").lower()).split()
    return (parts[0][0] + parts[-1]) if len(parts) >= 2 else "".join(parts)


def _prod_key_abbr(name: str) -> str:
    """'P.Nacua' -> 'pnacua' (nflverse abbreviated form)."""
    return re.sub(r"[.'\s]", "", (name or "").lower())


def _load_prior_production() -> dict:
    """{abbr_key: (ppg, games)} for the prior COMPLETED season. Pure data load, no AI.
    Returns {} on any failure so the guard degrades to inert (no suppression)."""
    try:
        from backend.integrations.nfl_data import NflDataWarehouse
        wh = NflDataWarehouse.build()
        season = get_current_season() - 1
        df = wh.get_seasonal_stats(season)
        out: dict = {}
        if df is None or df.empty:
            return out
        for _, r in df.iterrows():
            games = int(r.get("games", 0) or 0)
            if games <= 0:
                continue
            ppg = float(r.get("fantasy_points_ppr", 0) or 0) / games
            out[_prod_key_abbr(str(r.get("player_name", "")))] = (ppg, games)
        return out
    except Exception as exc:  # noqa: BLE001 — guard must never abort the valuation pass
        logger.warning("Displaced guard: prior production load failed (%s); guard inert", exc)
        return {}


def _apply_dependency_adjustment(
    ppr: float,
    dependencies: list,
    player_name: str | None = None,
    prior_production: dict | None = None,
    suppressed_log: list | None = None,
) -> float:
    """
    Apply pre-draft dependency flag adjustments to projected PPR.

    Rules:
    - BENEFICIARY + departed_team → apply immediately (positive)
    - DISPLACED + active_and_healthy → apply immediately (negative), UNLESS the
      displaced-direction guard fires (flagged player out-produced the trigger)
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
            # STEP 4 direction guard — skip a NEGATIVE displaced adj when the flagged
            # player out-produced the trigger per-game last season (both real samples).
            if impact < 0 and prior_production and isinstance(player_name, str):
                fp = prior_production.get(_prod_key_full(player_name))
                tp = prior_production.get(_prod_key_full(dep.trigger_player_name or ""))
                if (
                    fp and tp
                    and fp[1] >= _DISPLACED_GUARD_MIN_GAMES
                    and tp[1] >= _DISPLACED_GUARD_MIN_GAMES
                    and fp[0] > tp[0]
                ):
                    logger.warning(
                        "DISPLACED GUARD suppressed %+.0f%% on %s (%.1f ppg / %dg) "
                        "vs trigger %s (%.1f ppg / %dg) — flagged out-produced trigger",
                        impact * 100, player_name, fp[0], fp[1],
                        dep.trigger_player_name, tp[0], tp[1],
                    )
                    if suppressed_log is not None:
                        suppressed_log.append({
                            "player": player_name, "trigger": dep.trigger_player_name,
                            "player_ppg": round(fp[0], 1), "trigger_ppg": round(tp[0], 1),
                            "suppressed_pct": round(impact * 100, 0),
                        })
                    continue  # direction backwards — do NOT apply the negative adj
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
