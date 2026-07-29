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

Auction anchor — MARKET-FREE, PURE POOL-SHARE (ToS):

  recommended_bid_ceiling = system_value   (PAR pool-share, floored at $1, capped by position)

  The old two-value formula (market blend at anchor_weight + a tier-1 scarcity modifier) is
  removed: market no longer enters the anchor (ToS), and the scarcity modifier is dead. The
  market gap + risk-adjusted value are computed separately (value_gap, risk_adjusted_value).

  let_go_threshold = ceiling × LET_GO_MULTIPLIER[risk_level]

Anchor weights (market weight per tier — system-dominant):
  T1=0.30, T2=0.45, T3=0.55, T4=0.70, T5=0.80
Scarcity:       T1 RB=1.35, T1 WR=1.20, T1 QB/TE=1.10
Risk discounts:  low=0%, moderate=8%, high=15%, volatile=22%
Let-go:          low=1.20×, moderate=1.15×, high=1.10×, volatile=1.05×
"""
from __future__ import annotations

import logging
import math
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile, PlayerInjuryProfile
from backend.scoring import DEFAULT_FORMAT
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

# Positional budget allocation targets (% of LEAGUE_SKILL_DOLLAR_POOL).
#
# MUST SUM TO 1.0. The previous values (QB .10 / RB .38 / WR .32 / TE .10) summed to 0.90,
# which stranded $222 of a $2220 pool by construction — dollars no position could ever be
# allocated. Do NOT reintroduce a set that does not sum to 1.
#
# DERIVED, not chosen. Three completed seasons of real auction results from
# `league_auction_history` (~175 rows and ~$2340 of real money per season), weighted by
# recency 1/2/3. Seasons are named relatively on purpose — this file must hold no year
# literals (see test_analysis_year_dynamic_not_hardcoded):
#
#                  QB      RB      WR      TE
#   3 seasons ago 8.6%   45.1%   37.4%    9.0%
#   2 seasons ago 9.2%   38.3%   43.5%    9.1%
#   most recent   7.5%   36.5%   49.7%    6.2%
#   recency-w'ted 8.3%   38.5%   45.6%    7.6%   <- these numbers
#
# Recency-weighted rather than pooled: WR climbs monotonically (37.4 -> 43.5 -> 49.7) and
# RB falls (45.1 -> 38.3 -> 36.5) as PPR keeps shifting value to receivers. Pooling would
# treat the oldest season as equally informative about the coming one, and it is not.
#
# Do NOT invert WR and QB. QB is the SMALLEST share, not the second largest.
# Non-PPR needs no separate numbers: _format_budget_shares anchors on this dict.
POSITION_BUDGET_SHARE: dict[str, float] = {
    "QB": 0.083,
    "RB": 0.385,
    "WR": 0.456,
    "TE": 0.076,
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

# Minimum replacement-level PPR per game — BAD-DATA GUARD, never a value judgment.
# If the dynamically computed replacement PPR/game falls below these values, something is
# wrong with the data (too few profiles, skewed sample). A healthy board must NEVER see
# one of these bind: replacement is subtracted from every projection before the pool-share
# split, so a floor that binds silently steepens the position's entire dollar curve.
#
# DERIVED, NOT PICKED. One rule, every position, every format: the worst of the three
# completed seasons get_analysis_seasons(3) returns, at the position's OWN draftable-pool
# rank (get_draftable_pool_sizes: QB13 / RB52 / WR60 / TE18) — taken as the lowest value
# across four slot definitions (rank by season total; same with a >=8 game gate; rank by
# PPG at >=8 and at >=4 games, scaled to 17). Below that line the board is asserting
# something no recent season has produced at that slot, which is what "wrong data" means.
#
# PER FORMAT, because a single number cannot be right in three. Standard strips a full
# point per reception, so the same season-points bar is a far higher hurdle there. History
# is repriced through backend.scoring.season_points — the SAME function the board reprices
# with — so the guard and the thing it guards share one basis. QB is identical in all
# three by construction (no receptions).
#
# Measured on nflverse actuals — see docs/recon/replacement_floor_report.md.
#
# WHAT THIS REPLACED, and why it had to move: a single format-blind PPR-shaped set
# (QB 17.0 / RB 8.0 / WR 7.0 / TE 5.0), hand-picked when the guard was introduced and
# never calibrated. It shared no basis across positions — RB's asserted a replacement
# 28-57% above anything RB52 has scored (roughly RB44: a SECOND, contradictory statement
# of the pool depth get_draftable_pool_sizes already sets), while TE's sat so far below
# its own slot that it could never fire. Being format-blind it then bound on FIVE
# format x position pairs, worst of all on the Standard board it was never calibrated for:
#
#   ppr      QB +4    RB +18                    (the two the PPR-only view showed)
#   half_ppr QB +4    RB +27
#   standard QB +4    RB +36    WR +16          (RB replacement lifted 36%)
#
# On the live board the PPR RB floor alone zeroed the PAR of 8 of the 52 RBs inside the
# draftable pool and pushed the top RB into MAX_REALISTIC_BID, so a second rail began
# binding as a downstream consequence of the first.
#
# If you want a position's replacement to sit higher, change its DRAFTABLE POOL SIZE
# (_BENCH_SPLIT / _FLEX_SPLIT) — that is the honest lever and the one the TE fix used.
# Do not re-purpose this clamp for it.
#
# NOTE the sibling REPLACEMENT_LEVEL_MAX_PPR_PER_GAME below is still format-blind. It is
# inert in every format on the current board (checked), so it was left alone rather than
# disturb the recently-tuned TE cap — but it carries the same latent defect.
REPLACEMENT_FLOOR_PPG: dict[str, dict[str, float]] = {
    "ppr":      {"QB": 14.1, "RB": 4.8, "WR": 5.9, "TE": 7.2},
    "half_ppr": {"QB": 14.1, "RB": 4.1, "WR": 5.0, "TE": 5.7},
    "standard": {"QB": 14.1, "RB": 3.6, "WR": 3.8, "TE": 4.2},
}


def replacement_floor(position: str, scoring_format: str = DEFAULT_FORMAT) -> float:
    """The bad-data floor for one position in one format, in SEASON POINTS (PPG x 17).

    Always go through this rather than indexing REPLACEMENT_FLOOR_PPG directly: reading a
    PPR-shaped constant in a non-PPR context is precisely the bug this replaced. An
    unknown format falls back to PPR, which is the strictest row — a guard that errs
    toward firing is safe; one that errs toward silence is not.
    """
    table = REPLACEMENT_FLOOR_PPG.get(scoring_format) or REPLACEMENT_FLOOR_PPG[DEFAULT_FORMAT]
    return table.get(position, 0.0) * 17

# Maximum replacement-level PPR per game — prevents over-compression when
# profile data inflates bench player projections above realistic levels.
#
# THIS CAP IS A COMPRESSION LEVER, and a low one flattens the position's whole dollar
# curve. Replacement is SUBTRACTED from every projection before the pool-share split, so
# a cap that binds shrinks every PAR by the same amount and squashes the top toward the
# middle. Raise it only with the concentration table re-measured (see the TE note below).
REPLACEMENT_LEVEL_MAX_PPR_PER_GAME: dict[str, float] = {
    "QB": 22.0,   # ~374 season — streamable QB ceiling
    "RB": 10.0,   # ~170 season — waiver wire RB2 ceiling
    "WR": 9.0,    # ~153 season — waiver wire WR3 ceiling
    # 9.0 (~153 season), was 7.0 (~119). A 1-TE league drafts ~18 TEs, so replacement is
    # a low-end STARTER (measured 8.7 PPG at TE18), not a streamer. At 7.0 the cap
    # clamped replacement down to 119 and flattened the TE curve to half the market's
    # shape — the top five held 35.5% of TE money against the market's 56.6%, pricing the
    # best TE in football at $16 against a $31 market. Must move TOGETHER with
    # _BENCH_SPLIT["TE"]: at the old 25-deep pool the dynamic value (118.5) sat just
    # under the old cap, so raising this alone changes precisely nothing.
    "TE": 9.0,
}

# Injury recovery discount applied to PPR baseline for players with major injuries
POST_MAJOR_INJURY_DISCOUNT = 0.75  # 25% discount

# Default roster slots for a standard Yahoo PPR league
DEFAULT_ROSTER_SLOTS: dict[str, int] = {
    "QB": 1, "RB": 2, "WR": 2, "FLEX": 1, "TE": 1,
    "K": 1, "DEF": 1, "BENCH": 7,
}

# FLEX and bench allocation splits by position (empirical auction norms).
#
# These set the DRAFTABLE POOL SIZE, which is the single biggest lever on a position's
# value curve — the pool's last player IS the replacement level, so a pool that is too
# deep sets replacement too low and flattens the whole position. They do not need to sum
# to 1 (the remainder is bench spent on K/DEF and unrostered depth).
_FLEX_SPLIT: dict[str, float] = {"RB": 0.30, "WR": 0.60, "TE": 0.10}
_BENCH_SPLIT: dict[str, float] = {
    "QB": 0.08,   # unused: get_draftable_pool_sizes gives QB starters + 1
    "RB": 0.28,
    "WR": 0.35,
    # 0.06 (~5 bench TEs, an 18-deep pool), was 0.14 (~12 bench, a 25-deep pool). Twelve
    # BACKUP tight ends in a 1-TE league is not a real roster: teams stream the position.
    # That over-deep pool put replacement at TE25 (118.5 pts) instead of TE18 (148), and
    # since replacement is subtracted from every projection before the pool-share split,
    # the 30-point difference compressed the entire TE curve. Measured top1/top5 share of
    # the TE pool: 9.5%/37.3% before, 16.0%/58.6% after, against a market of 17.7%/56.6%.
    "TE": 0.06,
}

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

# SCARCITY_MODIFIERS removed: the tier-1 positional scarcity lift is dead (0.83 collinear
# with PAR, no out-of-sample signal, and it inflated elite RBs ~35% in the wrong direction).
# The anchor is now pure pool-share; positional_scarcity_modifier is always 1.00.

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
    # K/DEF have no clean_season_baseline and no projection chain, so there is no priced
    # points quantity to show. Written explicitly (not just left alone) to keep the
    # function idempotent for a player who changed position into K/DEF.
    player.adjusted_points              = None
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
    Compute the recommended bid ceiling — MARKET-FREE, PURE POOL-SHARE (ToS).

    The ceiling is now exactly the PAR pool-share ``system_value`` (floored at $1; the
    position cap is applied by the caller). TWO things were removed:
      * the FantasyPros/market blend (``ANCHOR_WEIGHTS[tier]`` weighted 0.30 T1 … 0.80 T5),
        which made the anchor 30–80% market by construction — the ToS close. #350 stripped
        market from the agent CONTEXT; this strips it from the ANCHOR.
      * the tier-1 positional scarcity modifier (RB 1.35 / WR 1.20 / …). Scarcity is dead
        (0.83 collinear with PAR, no out-of-sample signal) and it lifted elite RBs ~35% in
        the wrong direction (they are already the worst value-per-dollar). The board the
        held-out test validated (scripts/reshape_phase1_baseline.py,
        reshape_phase2_rb_stress.py) is pure pool-share with NO modifier; ship that.

    ``market_value`` / ``tier`` / ``position`` / ``risk_level`` are retained in the signature
    for call-site compatibility but are NO LONGER used here. Do NOT reintroduce a market or
    scarcity term.

    Args:
        system_value: PAR-derived auction dollar value (the pure pool-share anchor).
        market_value / tier / position / risk_level: IGNORED (signature compatibility).

    Returns:
        Decimal bid ceiling in dollars (minimum $1) == system_value floored at $1.
    """
    return _to_dec(max(Decimal("1.00"), system_value))


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


# Market-relative signal thresholds (dollars), applied to the gap =
# blind ai_bid_ceiling - market_value_fantasypros. The ±5 aligned band reuses the
# existing VALUE_GAP_*_THRESHOLD constants; ±15 is the "strong" band (mirrors the
# backtest's strong_buy/strong_avoid cut and classify_snake_flag's VALUE/REACH cut).
VALUE_GAP_ELITE_THRESHOLD = Decimal("15")    # ceiling >= market + 15 → elite_value / pay_up
VALUE_GAP_AVOID_THRESHOLD = Decimal("-15")   # ceiling <= market - 15 → avoid / nomination target


def derive_market_relative_signals(
    gap: Optional[Decimal],
) -> tuple[Optional[str], bool, bool]:
    """Deterministic (value_assessment, pay_up_flag, nomination_target_flag) from the
    BLIND ceiling-vs-market gap (ai_bid_ceiling - market_value_fantasypros).

    This is where market RE-ENTERS: the price opinion (ai_bid_ceiling) is formed blind by
    the agent; only afterward do we compare it to market to label how far off consensus it
    is. Monotonic in the gap, vocabulary unchanged (elite_value / good_value / fair_value /
    slight_overpay / avoid — the same set backtest.derive_system_signal consumes):

        gap >= +15  → elite_value,    pay_up=True   (we value them well above market)
        gap >= +5   → good_value
        -5 < gap<+5 → fair_value      (aligned with market)
        -15< gap<=-5→ slight_overpay
        gap <= -15  → avoid,          nomination_target=True   (market well above us)

    Returns (None, False, False) when there is no market to compare against (gap is None) —
    we make no market-relative claim rather than inventing a neutral one.
    """
    if gap is None:
        return None, False, False
    gap = _to_dec(gap)
    if gap >= VALUE_GAP_ELITE_THRESHOLD:
        return "elite_value", True, False
    if gap > VALUE_GAP_UNDERVALUE_THRESHOLD:      # > +5
        return "good_value", False, False
    if gap >= VALUE_GAP_OVERVALUE_THRESHOLD:      # -5 .. +5 inclusive band
        return "fair_value", False, False
    if gap > VALUE_GAP_AVOID_THRESHOLD:           # -15 < gap < -5
        return "slight_overpay", False, False
    return "avoid", False, True                   # gap <= -15


# Every field the valuation surface writes, mapped to its UNVALUED value. A player the
# pass declines to value must have ALL of them reset.
#
# WHY A NAMED SET: this used to be an inline list that cleared the math fields and
# nothing else, so a player who lost his profile kept `ai_bid_ceiling`, his auction note
# and his whole snake surface. The result is a GHOST — no anchor, no tier, no gap, but a
# stale dollar price the board still renders and, once positional budgets landed, that
# `apply_board_budgets` still funds out of a real position's pool. Measured on production:
# 20 ghosts holding $30, and 56 holding $123 before the profile backfill.
#
# pay_up_flag / nomination_target_flag are NOT NULL with a False default, so their
# unvalued value is False rather than None.
_UNVALUED_FIELDS: dict[str, object] = {
    # the math
    "tier": None, "adjusted_points": None, "baseline_value": None,
    "ceiling_value": None, "floor_value": None, "risk_adjusted_value": None,
    "recommended_bid_ceiling": None, "let_go_threshold": None,
    "elite_anchor_weight": None, "positional_scarcity_modifier": None,
    # the model's auction opinion
    "ai_bid_ceiling": None, "ai_confidence_floor": None,
    "ai_confidence_ceiling": None, "auction_note": None,
    # the market-relative judgement
    "value_gap": None, "value_gap_signal": None, "value_assessment": None,
    "signal_conviction": None, "pay_up_flag": False, "nomination_target_flag": False,
    # the snake surface
    "adp_ai": None, "adp_rank": None, "adp_diff": None, "snake_flag": None,
    # provenance
    "data_confidence": "low",
}


def clear_player_valuation(player) -> bool:
    """Reset every valuation field on a player the pass did not value.

    Returns True when something actually changed, so the caller can count real clears.

    The check is per-field rather than gated on one sentinel column. The old code only
    cleared `if player.baseline_value is not None`, which meant an ALREADY-half-cleared
    row — the exact ghost state, baseline gone but ceiling still set — could never be
    repaired: the gate saw a null baseline and skipped it forever. Idempotent: a second
    pass over a cleared player changes nothing and returns False.
    """
    changed = False
    for field, unvalued in _UNVALUED_FIELDS.items():
        if getattr(player, field, unvalued) != unvalued:
            setattr(player, field, unvalued)
            changed = True
    return changed


def apply_budget_gate(
    pay_up: bool, nomination: bool, ai_bid_ceiling, market,
) -> tuple[bool, bool]:
    """Suppress an ACTION flag that contradicts our own bid ceiling.

    PAY UP and NOMINATE are the two flags that tell a user to DO something, and they sit
    on the board next to the dollar gap (``ai_bid_ceiling - market``). They are derived
    from conviction, which is a within-position statement about production versus price
    and carries no budget constraint — so a player can be genuinely underpriced for his
    production AND still cost more than the pool allocates to him. Both statements are
    true; rendering them adjacent reads as the board contradicting itself.

    Measured on the board this was written against: 3 of 13 PAY UP players had a ceiling
    BELOW market — "pay up" next to "we would not pay that". The mirror case (NOMINATE on
    a player we would happily outbid) was 0 of 20, but it is the same defect and is gated
    symmetrically rather than left to luck of the data.

    Conviction still chooses the candidates and still drives ``value_assessment`` /
    ``value_gap_signal``, which are OPINIONS and may legitimately disagree with our
    budget. Only the two act-now flags are gated. Missing data gates nothing — with no
    ceiling or no market there is no contradiction to resolve.
    """
    if ai_bid_ceiling is None or market is None:
        return pay_up, nomination
    ceiling = float(ai_bid_ceiling)
    price = float(market)
    if pay_up and ceiling < price:
        pay_up = False
    if nomination and ceiling > price:
        nomination = False
    return pay_up, nomination


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
    position: str = "?",
) -> float:
    """
    Replacement level = projected PPR of the last player in the draftable pool.

    Args:
        sorted_pprs: PPR values sorted descending.
        pool_size: Number of players drafted at this position.
        position:   For logging only — names the position in the short-pool warning.

    Returns the replacement-level PPR (season total).

    SHORT POOL IS THE REAL BAD-DATA CASE. When fewer players are valued than the pool
    holds, the last element is returned instead — a number computed at the wrong rank,
    which then sets the steepness of the whole position's dollar curve. That is exactly
    the "too few profiles" failure REPLACEMENT_FLOOR_PPG was standing in for,
    and it used to pass silently: the magnitude floor could only catch it by accident,
    and only when the wrong-rank value happened to land low. Warn LOUDLY instead — a
    guard you can see fire is worth more than one that quietly rewrites the board.
    """
    if not sorted_pprs:
        return 0.0
    if len(sorted_pprs) >= pool_size:
        return sorted_pprs[pool_size - 1]
    logger.warning(
        "%s replacement computed on a SHORT POOL: %d players valued, pool needs %d. "
        "Using rank %d (%.1f) instead of rank %d — the whole %s dollar curve is built "
        "on a replacement level measured at the wrong rank. Check profile coverage.",
        position, len(sorted_pprs), pool_size,
        len(sorted_pprs), sorted_pprs[-1], pool_size, position,
    )
    return sorted_pprs[-1]


# How far a position's realized share of the draftable pool may sit from its budget
# target before the sanity check complains. Wider than the 2-point bar
# apply_board_budgets is verified to, because this runs on `baseline_value` — the
# pool-share math BEFORE the LLM and before enforcement — so some drift is expected and
# only a real divergence between the math and the budget is worth a warning.
_SANITY_SHARE_TOLERANCE_PTS = 6.0


def sanity_check_valuations(
    valued_players: list,
    league_pool: float = DEFAULT_LEAGUE_CONFIG.total_skill_pool,
) -> list[str]:
    """
    Post-valuation sanity checks. Returns list of warning strings.
    Empty list = all checks passed.

    SCALE-FREE BY CONSTRUCTION. Every bound here derives from POSITION_BUDGET_SHARE and
    the draftable pool rather than being a hardcoded dollar figure, because hardcoded
    figures silently go stale the moment the shares move — and a check that always warns
    is a check nobody reads. Two of the three warnings this emitted on production were
    artifacts of exactly that:

      * the total was summed over EVERY valued row, including the ~500-row $1 depth tail
        that no auction ever buys, so it exceeded the pool by construction and could never
        pass (it had been firing continuously);
      * the per-position bounds were absolute dollar averages over that same
        tail-dominated population, which makes them a restatement of
        ``share x pool / row_count`` — they were calibrated when QB was .10 and TE .10,
        and broke when those became .083 and .076.

    Args:
        valued_players: Player objects with baseline_value set.
        league_pool: Expected total dollar pool.
    """
    warnings: list[str] = []
    pool_sizes = get_draftable_pool_sizes()

    by_pos: dict[str, list[float]] = {}
    for p in valued_players:
        by_pos.setdefault(p.position, []).append(float(p.baseline_value or 0))

    # The BUDGETED population: each position's draftable pool, the same top-N
    # apply_board_budgets allocates over. The tail below it is $1 depth, never bought.
    pool_spend = {
        pos: sum(sorted(values, reverse=True)[:pool_sizes.get(pos, len(values))])
        for pos, values in by_pos.items()
    }
    total_pool_spend = sum(pool_spend.values())

    # Check 1: the draftable pool should cost about the league pool.
    if total_pool_spend > league_pool * 1.10:
        warnings.append(
            f"Draftable-pool value ${total_pool_spend:.0f} exceeds "
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

    # Check 3: a sane number of players priced above the $1 depth tail. Expressed as a
    # fraction of the draftable pool, not an absolute count, so it survives a shares or
    # roster-size change.
    total_pool_size = sum(pool_sizes.get(pos, 0) for pos in by_pos) or 1
    above_10 = sum(1 for vals in by_pos.values() for v in vals if v > 10)
    lo_n, hi_n = int(total_pool_size * 0.25), int(total_pool_size * 1.05)
    if not (lo_n <= above_10 <= hi_n):
        warnings.append(
            f"Unusual distribution: {above_10} players above $10 "
            f"(expected {lo_n}-{hi_n}, i.e. 25-105% of the {total_pool_size}-player pool)"
        )

    # Check 4: each position's pool should hold roughly its BUDGET SHARE of the spend.
    # This is the same quantity apply_board_budgets enforces, so a drift here means the
    # math and the enforcement disagree — which is the failure worth hearing about.
    share_sum = sum(POSITION_BUDGET_SHARE.values()) or 1.0
    if total_pool_spend > 0:
        for pos, spent in sorted(pool_spend.items()):
            share = POSITION_BUDGET_SHARE.get(pos)
            if not share:
                continue
            realized = spent / total_pool_spend * 100
            target = share / share_sum * 100
            if abs(realized - target) > _SANITY_SHARE_TOLERANCE_PTS:
                warnings.append(
                    f"{pos} holds {realized:.1f}% of the draftable pool against a "
                    f"{target:.1f}% target ({realized - target:+.1f} points)"
                )

    return warnings


# ---------------------------------------------------------------------------
# POSITIONAL BUDGET ENFORCEMENT
# ---------------------------------------------------------------------------
# The auction pool is finite and every position competes for the SAME dollars, so the
# per-position totals are a constraint, not a suggestion. This is the single place that
# constraint is applied, and it applies to EVERY position.
#
# WHY THIS EXISTS. `ai_bid_ceiling` — the number the draft board shows — is written by
# the LLM stage (agents/valuation_agent.py), which forms it as an independent opinion and
# may deviate from the math anchor. The only sum-to-budget rail used to live in
# `_apply_hybrid_rails` scoped to the RECEPTION positions (RB/WR/TE), because budget
# enforcement had piggybacked on `_FORMAT_INVARIANT_POSITIONS` / `_TIER_BAND_POSITIONS`.
# Those sets exist because QB POINTS do not change by scoring format — QBs don't catch
# passes. That is a real property and it is preserved. But budget enforcement is NOT
# format math and never should have inherited the exclusion: QB realized 15-21% of the
# board against a 10% target and the board totalled $3556 against a $2220 pool.
#
# WHY WATER-FILLING, not a single rescale: a naive scale fights the clamps. Scale the
# position down to its target, clamp the top players back up off the $1 floor / down off
# MAX_REALISTIC_BID, and the sum no longer equals the target — the clamped dollars just
# vanish (or appear). Water-filling pins whatever hit a bound, then redistributes the
# residual over the players still free to move, and repeats until nothing new clamps.
#
# WHICH PLAYERS THE BUDGET IS MEASURED OVER — the DRAFTABLE POOL. `budgeted` is each
# position's `get_draftable_pool_sizes` top-N: the same cut the replacement level and the
# z-tiers are already built on, and the ~150 skill players a 12-team league actually buys.
# The board prices ~673. Charging the $1 depth tail against the pool is both a category
# error (nobody buys them, so they never spend league money) and arithmetically
# degenerate: TE has 146 priced rows against a 7.6% share, so a whole-population
# constraint leaves $23 above the floor to split among all of them and prices the best TE
# in football at $3. The tail rides the SAME scale factor and floors at $1, so the
# ordering stays continuous across the pool boundary and no visible player is dropped.
# ---------------------------------------------------------------------------

_BUDGET_MAX_ITER = 20


def enforce_position_budget(
    values,
    target: float,
    cap: float,
    floor: int = 1,
    budgeted: Optional[int] = None,
    max_iter: int = _BUDGET_MAX_ITER,
) -> list[int]:
    """Water-fill ONE position's dollar values onto `target`. Pure — no DB, no ORM.

    Args:
        values:   dollar values, any order. Returned in the SAME order.
        target:   dollars the budgeted subset must sum to.
        cap:      per-player maximum (MAX_REALISTIC_BID for the position).
        floor:    per-player minimum ($1 — a drafted player always costs something).
        budgeted: how many of the HIGHEST values count toward `target` (the draftable
                  pool). None = all of them. Entries outside it are scaled identically
                  but do not spend the budget.

    Returns integers in ``[floor, cap]`` whose budgeted subset sums to ``round(target)``
    exactly (largest-remainder allocation), preserving the input ordering.

    Infeasible targets are honoured as far as the bounds allow rather than faked: a
    position of n players cannot cost less than ``n * floor``, so a target below that
    returns the floor and the caller sees the overshoot.
    """
    n = len(values)
    if n == 0:
        return []

    cap_f = float(cap)
    floor_f = float(floor)
    vals = [max(0.0, float(v or 0.0)) for v in values]

    # Budget membership by rank on the INPUT values (stable on ties by index).
    k = n if budgeted is None else max(0, min(int(budgeted), n))
    order = sorted(range(n), key=lambda i: (-vals[i], i))
    in_budget = [False] * n
    for i in order[:k]:
        in_budget[i] = True

    out = list(vals)
    pinned = [False] * n

    for _ in range(max_iter):
        fixed = sum(out[i] for i in range(n) if in_budget[i] and pinned[i])
        free = sum(out[i] for i in range(n) if in_budget[i] and not pinned[i])
        remaining = target - fixed
        if free <= 0 or remaining <= 0:
            # Every budgeted player is pinned (or worthless): the bounds, not the scale,
            # now decide the total. Nothing left to redistribute.
            break
        scale = remaining / free
        for i in range(n):
            if not pinned[i]:
                out[i] *= scale
        newly_pinned = False
        for i in range(n):
            if pinned[i]:
                continue
            if out[i] > cap_f:
                out[i] = cap_f
                pinned[i] = True
                newly_pinned = True
            elif out[i] < floor_f:
                out[i] = floor_f
                pinned[i] = True
                newly_pinned = True
        if not newly_pinned:
            break

    return _integer_allocation(out, in_budget, target, floor, int(cap))


def _integer_allocation(
    out: list[float], in_budget: list[bool], target: float, floor: int, cap: int,
) -> list[int]:
    """Round the water-filled floats to whole dollars without losing the target.

    ``ai_bid_ceiling`` is an integer column, and rounding 150 players independently moves
    the total by more than the 2% the budget is verified to. Largest-remainder over the
    BUDGETED subset makes the integer sum equal ``round(target)`` exactly.

    Ordering survives: floor(a) >= floor(b) whenever a >= b, and a +1 only ever goes to
    the larger fractional part, so within one integer step the higher value is always
    served first and can at worst be equalled. The tail rounds DOWN (never up), which
    cannot lift a tail player past the pool player above him.
    """
    n = len(out)
    result = [0] * n
    frac: list[float] = [0.0] * n
    for i in range(n):
        base = int(math.floor(out[i]))
        result[i] = max(floor, min(base, cap))
        frac[i] = out[i] - base

    budget_idx = [i for i in range(n) if in_budget[i]]
    if not budget_idx:
        return result

    deficit = int(round(target)) - sum(result[i] for i in budget_idx)

    if deficit > 0:
        # Hand out the remaining dollars, largest fractional part first.
        candidates = sorted(budget_idx, key=lambda i: (-frac[i], -out[i], i))
        while deficit > 0:
            eligible = [i for i in candidates if result[i] < cap]
            if not eligible:
                break
            for i in eligible:
                if deficit == 0:
                    break
                result[i] += 1
                deficit -= 1
    elif deficit < 0:
        # Take dollars back, smallest fractional part first.
        candidates = sorted(budget_idx, key=lambda i: (frac[i], out[i], i))
        while deficit < 0:
            eligible = [i for i in candidates if result[i] > floor]
            if not eligible:
                break
            for i in eligible:
                if deficit == 0:
                    break
                result[i] -= 1
                deficit += 1

    return result


def apply_board_budgets(
    rows,
    total_budget: float,
    shares: Optional[dict[str, float]] = None,
    pool_sizes: Optional[dict[str, int]] = None,
    caps: Optional[dict[str, int]] = None,
) -> dict:
    """Enforce every position's budget across a whole board. Pure — no DB.

    Args:
        rows:         mappings with ``key`` (any hashable id), ``position``, ``value``.
        total_budget: the league skill pool.
        shares:       per-position budget share. Renormalised over the WHOLE dict, so a
                      share set that sums to less than 1 still allocates the whole pool
                      instead of stranding the difference. Renormalising over only the
                      positions PRESENT would be wrong: a board missing a position would
                      silently hand that position's pool to the others.
        pool_sizes:   per-position draftable pool size (the budgeted subset).
        caps:         per-position maximum bid.

    Returns ``{key: int}``. Positions with no budget share (K/DEF — $1 streamers valued
    on a separate static path) are bounded to ``[1, cap]`` and never rescaled into the
    skill pool.
    """
    shares = POSITION_BUDGET_SHARE if shares is None else shares
    pool_sizes = get_draftable_pool_sizes() if pool_sizes is None else pool_sizes
    caps = MAX_REALISTIC_BID if caps is None else caps

    by_pos: dict[str, list[dict]] = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r)

    share_sum = sum(shares.values()) or 1.0
    result: dict = {}
    for pos, group in by_pos.items():
        cap = caps.get(pos, 80)
        share = shares.get(pos)
        if not share:
            # No budget share: bound only. K/DEF are static $1 streamers.
            for r in group:
                result[r["key"]] = max(1, min(int(round(float(r["value"] or 1))), cap))
            continue
        target = total_budget * share / share_sum
        enforced = enforce_position_budget(
            [r["value"] for r in group],
            target=target,
            cap=cap,
            budgeted=pool_sizes.get(pos),
        )
        for r, v in zip(group, enforced):
            result[r["key"]] = v
    return result


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

    # MARKET-FREE (ToS): the anchor no longer blends market on ANY path. Pass market_value=None
    # explicitly so no FantasyPros/ADP value can reach the ceiling (compute_bid_ceiling ignores
    # it regardless — belt and suspenders).
    ceiling = compute_bid_ceiling(sv, None, tier, pos, risk_level)
    max_bid_dec = Decimal(str(MAX_REALISTIC_BID.get(pos, 80)))
    if ceiling > max_bid_dec:
        ceiling = max_bid_dec

    let_go = compute_let_go_threshold(ceiling, risk_level)
    risk_adj = _to_dec(sv * (Decimal("1") + (rm or Decimal("0"))))
    anchor = ANCHOR_WEIGHTS.get(tier, Decimal("0.00"))
    scarcity = Decimal("1.00")  # positional scarcity modifier dropped — pure pool-share

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
        # Capped to the SAME position maximum as recommended_bid_ceiling above. These two
        # are rendered side by side ("System" and "Bid Ceiling" on the detail panel), and
        # an uncapped baseline read as a system opinion the ceiling was overriding: Josh
        # Allen showed System $73.38 against a $50 QB cap. The cap is a league-rules bound
        # on what any player can cost, so it applies to both or neither.
        "baseline_value": min(sv, max_bid_dec),
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
        # STEP 4b — who counts as a real displacer. Built once over the whole pool.
        credible_triggers = _build_credible_triggers(players)

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
                    credible_triggers=credible_triggers,
                    projection_prices_flags=_projection_prices_flags(player),
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
            dynamic_repl = calculate_replacement_level(sorted_pprs, pool_size, pos)

            # Enforce replacement level bounds (PPG × 17 games). This pass is PPR.
            floor_ppr = replacement_floor(pos)
            max_ppr = REPLACEMENT_LEVEL_MAX_PPR_PER_GAME.get(pos, 15.0) * 17
            repl_ppr = min(max(dynamic_repl, floor_ppr), max_ppr)
            if repl_ppr > dynamic_repl and repl_ppr == floor_ppr:
                repl_name = "?"
                if len(sorted_pprs) >= pool_size:
                    # Find the replacement player's name for logging
                    repl_name = group[pool_size - 1][0].name
                # WARNING, not INFO: this floor is calibrated never to bind on a healthy
                # board, so a firing means the projections are suspect — and it silently
                # steepens the whole position's dollar curve. It must not scroll past.
                logger.warning(
                    "%s replacement floor enforced [ppr]: dynamic=%.1f "
                    "(#%d %s) < floor=%.1f — the %s curve is now built on the floor, "
                    "not on the board's own projections. Check the projections.",
                    pos, dynamic_repl, pool_size, repl_name, floor_ppr, pos,
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
                # The points the dollars below were computed from. Persisted so the PPR
                # surfaces can display the priced quantity instead of the raw projection
                # (see the column docstring on Player.adjusted_points).
                player.adjusted_points            = _to_dec(round(adjusted_ppr, 1))
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
                if clear_player_valuation(player):
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


async def enforce_ai_ceiling_budgets(
    config: LeagueConfig = DEFAULT_LEAGUE_CONFIG, dry_run: bool = False,
) -> dict:
    """Rail the players table's ``ai_bid_ceiling`` onto the positional budget.

    MUST RUN AFTER ``valuation_agent``. ``ai_bid_ceiling`` is the LLM's number and the one
    the draft board shows; ``recommended_bid_ceiling`` is the pool-share math underneath
    it. Enforcing inside ``run_valuation_pass`` would only touch the second, which the
    agent then overwrites — the enforcement would be invisible on the board.

    Also runs before ``run_prose_for_format``, which copies the format-invariant QB/K/DEF
    opinions verbatim out of this table into the per-format rows.

    Pure Python over rows already loaded: no API calls, no cache invalidation.
    """
    total_budget = float(config.total_skill_pool)
    pool_sizes = get_draftable_pool_sizes(config.team_count)

    before: dict[str, float] = {}
    after: dict[str, float] = {}
    updated = 0
    async with AsyncSessionLocal() as session:
        # An anchor is REQUIRED, not just a ceiling. A row with a ceiling and no
        # recommended_bid_ceiling is a ghost — the valuation pass declined to value the
        # player, so he is not in any position's draftable pool and must not draw from
        # its budget. clear_player_valuation now strips those rows, but requiring the
        # anchor here means a ghost created by some future path still cannot be funded.
        players = (await session.execute(
            select(Player).where(
                Player.ai_bid_ceiling.isnot(None),
                Player.recommended_bid_ceiling.isnot(None),
            )
        )).scalars().all()
        rows = [
            {"key": p.id, "position": p.position, "value": float(p.ai_bid_ceiling)}
            for p in players
        ]
        enforced = apply_board_budgets(
            rows, total_budget, POSITION_BUDGET_SHARE, pool_sizes)

        for p in players:
            pos = p.position or "?"
            new = enforced.get(p.id)
            before[pos] = before.get(pos, 0.0) + float(p.ai_bid_ceiling)
            after[pos] = after.get(pos, 0.0) + float(new)
            if new is not None and new != p.ai_bid_ceiling:
                if not dry_run:
                    p.ai_bid_ceiling = new
                    session.add(p)
                updated += 1

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    logger.info(
        "Positional budget enforced on %d ai_bid_ceiling(s) (dry_run=%s): %s -> %s",
        updated, dry_run,
        {k: round(v) for k, v in sorted(before.items())},
        {k: round(v) for k, v in sorted(after.items())},
    )
    return {
        "updated": updated, "dry_run": dry_run,
        "before": before, "after": after,
        "pool": total_budget, "pool_sizes": pool_sizes,
    }


async def enforce_format_ai_ceiling_budgets(
    config: LeagueConfig = DEFAULT_LEAGUE_CONFIG, dry_run: bool = False,
) -> dict:
    """The same enforcement for ``player_format_values.ai_bid_ceiling`` (Half/Standard).

    MUST RUN AFTER ``run_prose_for_format``, which is what writes those ceilings.

    Targets come from ``_format_budget_shares``, so QB's share is held fixed across
    formats — which is precisely why enforcing here does not break format-invariance. QB
    points are identical in every format, its share is identical in every format, and the
    pool is identical, so the enforced QB dollars come out identical too. Before this, the
    format rows copied the players-table QB verbatim into a SMALLER non-PPR total and QB
    realized 22.9% of it against an 11.1% target.

    Aggregate PAR is recomputed from the stored per-format rows. That is the raw projected
    points rather than the injury/dependency-adjusted points the writer used, so the
    shares can differ from the write-time shares in the third decimal — far below the 2%
    the budget is verified to, and it keeps this pass independent of writer state.
    """
    from backend.models.player_format_values import PlayerFormatValues

    total_budget = float(config.total_skill_pool)
    pool_sizes = get_draftable_pool_sizes(config.team_count)
    results: dict[str, dict] = {}

    async with AsyncSessionLocal() as session:
        # EVERY row, not just the ones carrying a ceiling. The PPR format row has a NULL
        # ai_bid_ceiling by design (the players table is authoritative for PPR), so
        # filtering on it here would leave ppr_total_par empty and silently collapse
        # _format_budget_shares back to the PPR defaults — the format-aware RB/WR/TE
        # shift would never be applied.
        rows = (await session.execute(
            select(PlayerFormatValues.id, PlayerFormatValues.scoring_format,
                   PlayerFormatValues.ai_bid_ceiling, PlayerFormatValues.projected_points,
                   PlayerFormatValues.replacement_ppr, Player.position)
            .join(Player, Player.id == PlayerFormatValues.player_id)
        )).all()

        by_format: dict[str, list] = {}
        for r in rows:
            by_format.setdefault(r.scoring_format, []).append(r)

        def _total_par(rs) -> dict[str, float]:
            """Aggregate points-above-replacement per position, from the stored rows."""
            par: dict[str, float] = {}
            for r in rs:
                if r.projected_points is None or r.replacement_ppr is None:
                    continue
                par[r.position] = par.get(r.position, 0.0) + max(
                    0.0, float(r.projected_points) - float(r.replacement_ppr))
            return par

        ppr_par = _total_par(by_format.get("ppr", []))
        updated_ids: dict = {}
        for fmt, frows in by_format.items():
            priced = [r for r in frows if r.ai_bid_ceiling is not None]
            if not priced:
                continue
            shares = _format_budget_shares(fmt, ppr_par, _total_par(frows))
            enforced = apply_board_budgets(
                [{"key": r.id, "position": r.position, "value": float(r.ai_bid_ceiling)}
                 for r in priced],
                total_budget, shares, pool_sizes,
            )
            changed = 0
            for r in priced:
                new = enforced.get(r.id)
                if new is not None and new != r.ai_bid_ceiling:
                    updated_ids[r.id] = new
                    changed += 1
            results[fmt] = {"rows": len(priced), "updated": changed, "shares": shares}

        if not dry_run and updated_ids:
            for row_id, new in updated_ids.items():
                await session.execute(
                    update(PlayerFormatValues)
                    .where(PlayerFormatValues.id == row_id)
                    .values(ai_bid_ceiling=new)
                )
            await session.commit()
        else:
            await session.rollback()

    logger.info("Positional budget enforced on per-format ceilings (dry_run=%s): %s",
                dry_run, {f: r["updated"] for f, r in sorted(results.items())})
    return {"formats": results, "dry_run": dry_run}


async def reconcile_value_signals(dry_run: bool = False) -> dict:
    """DETERMINISTIC market-relative post-pass (Phase 6). Pure DB pass — NO AI calls.

    The PPR valuation agent is now MARKET-BLIND: it forms ai_bid_ceiling + auction_note with
    no market/ADP/prior-price in its context, so it can no longer honestly emit the
    market-relative fields. This pass is where MARKET RE-ENTERS — after the blind opinion
    exists — to compute, from the blind ai_bid_ceiling vs market_value_fantasypros:
      - value_gap + value_gap_signal (unchanged: compute_value_gap_from_player), AND
      - value_assessment / pay_up_flag / nomination_target_flag
        (NEW: derive_market_relative_signals — previously the model produced these; the
        blind model no longer does, and _write_results no longer writes them).

    This also inherently fixes the old stale-gap ordering bug (value_gap is recomputed against
    the FINAL ceiling) and makes pay_up non-contradictory (pay_up now requires ceiling >=
    market + $15, so "PAY UP at $44 vs $61" cannot render — it is generated, not merely
    suppressed). market_value_fantasypros is the sole market basis (consensus ADP, shared
    across users); the client chip's cheap/small-gap guards read league price separately and
    are unaffected.

    BASIS CHANGE (signal accuracy work). The judgement fields no longer derive from the
    dollar gap ``ai_bid_ceiling - market``. That basis was measured WORSE than the
    projection underneath it on the as-of prospective backtest — 55.6% vs 60.3% over 151
    priced players, with the ceiling itself at 51.7% (paired McNemar p = 0.029) and adding
    +0.0005 R² beyond the projection. They now derive from the standardised within-position
    residual of the projection against the market's own points-vs-price curve
    (``backend/engines/signal_basis.py``), which is price-neutral (corr with ln price
    +0.017, against −0.581 for the dollar gap). Full write-up:
    ``docs/recon/signal_accuracy_recon.md``.

    ``ai_bid_ceiling`` is UNCHANGED and remains the auction bid surface — bidding, budget
    maths and the live-draft engine are untouched. Ranking of "top opportunities" must use
    ``signal_conviction``, never ``value_gap``: the dollar magnitude is price-biased and
    the top 20% of the board by dollar gap scored 46.7%.

    TWO QUANTITIES, TWO COLUMNS, NEVER MIXED:

      ``value_gap``          DOLLARS. ``ai_bid_ceiling - market``. What we would bid
                             versus what he will cost — the auction question, zero-sum
                             against the market by construction (measured: our ceilings
                             $2334 against a $2357 market over the same 155 players) and
                             directly actionable per row.
      ``signal_conviction``  The standardised price-curve residual. Drives every
                             JUDGEMENT field and all ranking.

    ``value_gap`` briefly held the curve's ``dollar_edge`` instead. That was wrong to
    display as money: ``implied_price`` inverts a log-linear fit and so is clamped to
    1.5x the priciest player at the position, and 12 of 155 priced players sat AT that
    clamp — 30% of the top ten by price and 62% of PAY UP players. For them the printed
    "gap" was ``price_cap - price``, a pure function of price, ordering elite players by
    cheapness: CeeDee Lamb showed +$44 against a $39 market while our own ceiling was $40.
    ``dollar_edge`` was only ever guaranteed in its SIGN (see its docstring) and is no
    longer persisted anywhere.

    Still a pure DB pass — the curve fit is arithmetic over rows already loaded. NO API
    calls, no extra queries per player, no cache invalidation.
    """
    from backend.engines.signal_basis import (
        conviction_to_gap_signal,
        derive_signals_from_conviction,
        fit_price_curve,
    )

    updated = 0
    flag_counts = {"pay_up": 0, "nomination_target": 0}
    curve_stats: dict[str, int] = {}
    legacy_fallbacks = 0
    report: list[dict] = []
    async with AsyncSessionLocal() as session:
        players = (await session.execute(
            select(Player)
            .options(selectinload(Player.profile))
            .where(Player.ai_bid_ceiling.isnot(None))
        )).scalars().all()

        # ---- Fit the market's points-vs-price curve, once per position -------------
        # Pure arithmetic over rows already in memory: no query per player, no API call.
        by_pos: dict[str, list[tuple[float, float]]] = {}
        for p in players:
            pts = _extract_ppr(p.profile)
            mkt = getattr(p, "market_value_fantasypros", None)
            if pts and pts > 0 and mkt and float(mkt) > 0:
                by_pos.setdefault(p.position or "", []).append((pts, float(mkt)))
        curves = {
            pos: fit_price_curve([pt for pt, _ in rows], [pr for _, pr in rows])
            for pos, rows in by_pos.items()
        }
        curve_stats = {
            pos: (c.n if c is not None else 0) for pos, c in curves.items()
        }

        for p in players:
            old = (
                float(p.value_gap) if p.value_gap is not None else None,
                p.value_gap_signal,
                p.value_assessment,
                bool(p.pay_up_flag),
                bool(p.nomination_target_flag),
            )

            curve = curves.get(p.position or "")
            pts = _extract_ppr(p.profile)
            mkt = getattr(p, "market_value_fantasypros", None)
            conviction = (
                curve.conviction(pts, float(mkt))
                if (curve is not None and pts and pts > 0 and mkt and float(mkt) > 0)
                else None
            )

            if conviction is not None:
                # PRIMARY PATH — price-neutral projection residual (signal_basis) for the
                # JUDGEMENT. The DOLLAR column stays ceiling-vs-market on every path.
                assessment, pay_up, nomination = derive_signals_from_conviction(conviction)
                sig = conviction_to_gap_signal(conviction)
                gap, _ = compute_value_gap_from_player(p)
            else:
                # FALLBACK — no curve for this position (K/DEF, too few priced players)
                # or no projection/market for this player. Degrade to the legacy
                # dollar-gap basis rather than emitting nothing: never worse than before.
                legacy_fallbacks += 1
                gap, sig = compute_value_gap_from_player(p)
                assessment, pay_up, nomination = derive_market_relative_signals(gap)

            # BUDGET GATE — an ACTION flag must agree with our own bid. Conviction picks
            # the candidates; whether we would actually transact decides the badge.
            pay_up, nomination = apply_budget_gate(
                pay_up, nomination, p.ai_bid_ceiling, mkt)

            new_gap = float(gap) if gap is not None else None
            new = (new_gap, sig, assessment, pay_up, nomination)

            if pay_up:
                flag_counts["pay_up"] += 1
            if nomination:
                flag_counts["nomination_target"] += 1

            if dry_run:
                if old != new:
                    report.append({
                        "name": p.name,
                        "ai_bid_ceiling": float(p.ai_bid_ceiling) if p.ai_bid_ceiling is not None else None,
                        "market_fp": float(p.market_value_fantasypros) if p.market_value_fantasypros is not None else None,
                        "gap": [old[0], new_gap],
                        "signal": [old[1], sig],
                        "assessment": [old[2], assessment],
                        "pay_up": [old[3], pay_up],
                        "nomination_target": [old[4], nomination],
                    })
            else:
                p.value_gap = gap
                p.value_gap_signal = sig
                p.value_assessment = assessment
                p.pay_up_flag = pay_up
                p.nomination_target_flag = nomination
                p.signal_conviction = conviction
                session.add(p)
                updated += 1

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    return {
        "updated": updated,
        "flag_counts": flag_counts,
        "price_curves": curve_stats,
        "legacy_fallbacks": legacy_fallbacks,
        # Back-compat: the pipeline prints len(payup_suppressed); pay_up is now GENERATED
        # (only ever true at gap >= +15, never contradictory), so nothing is "suppressed".
        "payup_suppressed": [],
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
        # STEP 4b — who counts as a real displacer. Same pool every format.
        credible_triggers = _build_credible_triggers(players)

        async def _upsert(row: dict) -> None:
            nonlocal written
            if dry_run:
                dry_rows.append(row)
                written += 1
                return
            stmt = pg_insert(PlayerFormatValues).values(**row)
            # NOT named `update` — the module now imports sqlalchemy's update().
            set_values = {k: row[k] for k in row if k not in ("player_id", "scoring_format")}
            set_values["updated_at"] = _func.now()
            await session.execute(stmt.on_conflict_do_update(
                constraint="uq_player_format", set_=set_values))
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
                        credible_triggers=credible_triggers,
                        projection_prices_flags=_projection_prices_flags(player),
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
                dynamic_repl = calculate_replacement_level(sorted_pprs, pool_size, pos)
                # THIS FORMAT's floor, not PPR's. `group` holds points already repriced
                # into `fmt`, so a PPR-shaped floor would be a far higher bar here — that
                # mismatch bound on Standard RB/WR and Half RB before it was caught.
                floor_ppr = replacement_floor(pos, fmt)
                max_ppr = REPLACEMENT_LEVEL_MAX_PPR_PER_GAME.get(pos, 15.0) * 17
                repl_ppr = min(max(dynamic_repl, floor_ppr), max_ppr)
                if repl_ppr > dynamic_repl and repl_ppr == floor_ppr:
                    logger.warning(
                        "%s replacement floor enforced [%s]: dynamic=%.1f < floor=%.1f — "
                        "the %s %s curve is built on the floor, not the projections.",
                        pos, fmt, dynamic_repl, floor_ppr, fmt, pos,
                    )
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
    - decline: 15% — ONLY when the projection is backward-looking. A forward Sonnet
      projection has already priced the decline (see below), so re-applying it there
      double-counts. Sources: the model's career_trajectory label, or the
      Python-computed clean_season_baseline["declining"] flag.
    """
    discount = 1.0

    # Check injury profile for major injury flags
    if injury_profile:
        if injury_profile.post_acl_flag:
            discount *= POST_MAJOR_INJURY_DISCOUNT
        elif injury_profile.workload_cliff_flag:
            discount *= 0.85  # 15% discount for workload cliff

    # Career decline. Both sources are gated on the SAME condition that gates the
    # dependency flags: a forward Sonnet projection has already priced this in.
    #
    # career_trajectory is written by the same Sonnet call that writes
    # projected_ppr_season, so a "declining" label and a lowered projection are two
    # renderings of one judgement. Measured on the live board as projection / the
    # player's OWN historical ppr_points:
    #     label only         n=91   mean 0.678   median 0.668   <- already cut 32%
    #     label + py flag    n=59   mean 0.973   median 0.848
    #     python flag only   n=20   mean 1.305   median 1.215   <- projected UP
    #     neither            n=237  mean 1.056   median 1.005
    # The labelled group is already projected down a third; the engine then took
    # another 15%. Josh Allen 393.8 -> 368.0 -> 312.8; Travis Kelce 217.3 -> 182.4 ->
    # 155.0. With the QB replacement floor at 289 this was a cliff: Lamar Jackson and
    # Patrick Mahomes fell below replacement to a $1.00 anchor on the second cut alone.
    #
    # The python-flag-only row is the same error in reverse — those players were
    # projected UP 30%, and the historical flag discounted them anyway. When a forward
    # projection exists it supersedes the backward-looking flag, so both branches are
    # gated together rather than trading one for the other.
    #
    # A backward-looking profile (nfl_history / college_comps / the K/DEF paths) never
    # made that judgement, so for those the discount is the only pricing of decline and
    # still applies.
    if profile and not _projection_prices_decline(profile):
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

# STEP 4b — displaced SUBSTANTIATION guard. The direction check above can only fire when
# the trigger HAS prior production to compare against, so the most obviously bogus triggers
# — players who do not exist as NFL contributors at all — sailed straight through it. That
# is how Ja'Marr Chase and Tee Higgins both carried a -30% "displaced by Dohnte Meyers"
# (a CIN camp body: no profile, no projection, no depth-chart order, no draft capital),
# which alone cut Chase's PPR anchor from ~$28 to $12.91.
#
# A displaced flag asserts the trigger is a SUPERIOR player. That claim needs the trigger to
# be SOMEBODY: either he produced in the NFL last season (prior_production), or he carries a
# real forward projection in our own pool (credible_triggers — which a genuine rookie
# displacer gets via the rookie projection path). A trigger in neither set is unsubstantiated
# and its NEGATIVE adjustment is dropped.
#
# Direction of error is deliberate: suppressing means declining to apply a haircut we cannot
# substantiate. A name we simply failed to match costs us a legitimate penalty; a hallucinated
# trigger we honor costs an elite player 30% of his value. The former is far cheaper.
# Both guards are inert when their data is absent, so existing callers/tests are unaffected.


# Generational suffixes must be stripped BEFORE taking the surname. Without this
# _prod_key_full('Kenneth Walker III') keyed to 'kiii' and 'Deebo Samuel Sr.' to 'dsr',
# so no suffixed player ever matched prior_production — silently disabling the displaced
# direction guard for every one of them. Worse, the garbage keys COLLIDE: 'Marvin Harrison
# Jr.' and 'Michael Pittman Jr.' both produced 'mjr', so one could answer for the other.
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv"})


def _prod_key_full(name: str) -> str:
    """'Puka Nacua' -> 'pnacua' (first initial + surname, punctuation- and suffix-stripped).

    'Kenneth Walker III' -> 'kwalker', matching _prod_key_abbr('K.Walker').
    """
    parts = re.sub(r"[.']", "", (name or "").lower()).split()
    while len(parts) > 2 and parts[-1] in _NAME_SUFFIXES:
        parts.pop()
    # A 2-token name that is first + suffix ('Deebo Sr.') has no surname to key on;
    # keep it as-is rather than inventing one.
    if len(parts) == 2 and parts[-1] in _NAME_SUFFIXES:
        return "".join(parts)
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
        collisions = 0
        for _, r in df.iterrows():
            games = int(r.get("games", 0) or 0)
            if games <= 0:
                continue
            ppg = float(r.get("fantasy_points_ppr", 0) or 0) / games
            key = _prod_key_abbr(str(r.get("player_name", "")))
            # first-initial+surname is NOT unique (15 colliding keys in a typical season:
            # two T.Etiennes, three J.Williamses, ...). A plain assignment kept whichever
            # row happened to land last, so the guard could compare against a phantom —
            # it judged Alvin Kamara against a 1.5 ppg / 8 g "T.Etienne" instead of the
            # real 14.9 ppg / 17 g one. Keep the MOST-GAMES row, the established tiebreak
            # for this codebase's name fallbacks (see _get_player_season_stats).
            prev = out.get(key)
            if prev is not None:
                collisions += 1
                if prev[1] >= games:
                    continue
            out[key] = (ppg, games)
        if collisions:
            logger.info(
                "Displaced guard: %d colliding name keys in prior production — kept the "
                "most-games row for each. Name keys are ambiguous by construction; an "
                "ID-first join (rule #7) would remove the ambiguity entirely.",
                collisions,
            )
        return out
    except Exception as exc:  # noqa: BLE001 — guard must never abort the valuation pass
        logger.warning("Displaced guard: prior production load failed (%s); guard inert", exc)
        return {}


# Profile sources whose PROJECTION ALREADY PRICES the dependency flags.
#
# player_profiles runs LAST in the pipeline (CLAUDE.md step 7, after roster_changes at
# step 3) and both Sonnet prompts are handed the flags and told what to do with them:
#   "A beneficiary flag with departed_team trigger = MORE opportunity -> project HIGHER
#    than historical baseline"
#   "A displaced flag with active_and_healthy trigger = LESS opportunity -> project LOWER"
# (backend/agents/player_profiles.py; the rookie prompt gets a DEPENDENCY FLAGS block too.)
#
# So projected_ppr_season already contains the flag's effect, and the engine was applying
# it a SECOND time. Measured on the live board — projection lift over each player's own
# historical ppr_points, by group:
#     beneficiary+departed   n=50   mean +27.9%   median +14.4%   76% above history
#     displaced+active       n=90   mean  -9.4%   median -11.6%   30% above history
#     unflagged (baseline)   n=267  mean  -6.4%   median  -6.6%   37% above history
# Beneficiaries sit ~34 points above the unflagged baseline — almost exactly the +35% the
# engine then re-applied. Josh Downs: historical 155.7 -> projected 198.4 (+27.4%), with
# projection_reasoning naming the Pittman and Mitchell departures verbatim; the engine then
# made it 337.3 and turned a $6-market receiver into the $33 WR1.
#
# The Python projection paths (nfl_history, college_comps, kicker_*, defense_history) never
# see the flags, so for those the engine adjustment is the ONLY pricing of them and must
# still apply. That is what this set discriminates.
_PROJECTION_PRICES_FLAGS_SOURCES = frozenset({"sonnet_projection", "sonnet_rookie"})


def _projection_prices_flags(player) -> bool:
    """True when this player's projection already priced his dependency flags."""
    profile = getattr(player, "profile", None)
    return bool(profile) and getattr(profile, "profile_source", None) in _PROJECTION_PRICES_FLAGS_SOURCES


def _projection_prices_decline(profile) -> bool:
    """True when this PROFILE's projection already priced a career decline.

    Takes the profile directly (not the player) because _apply_injury_discount is handed
    a profile. Requires BOTH a Sonnet source and an actual forward projection: the source
    alone is not enough, because _extract_ppr falls back to the historical ppr_points when
    projected_ppr_season is absent or zero, and in that case nothing has priced the
    decline. Note the falsy check rather than `is None` — it must match _extract_ppr's
    `or`, which treats a 0.0 projection as absent.
    """
    if getattr(profile, "profile_source", None) not in _PROJECTION_PRICES_FLAGS_SOURCES:
        return False
    baseline = getattr(profile, "clean_season_baseline", None) or {}
    try:
        return float(baseline.get("projected_ppr_season") or 0) > 0
    except (TypeError, ValueError):
        return False


def _build_credible_triggers(players: list) -> set:
    """Production keys for every player carrying a real forward projection.

    A displaced trigger present here is SOMEBODY — he has a clean_season_baseline with
    positive projected points, which is what a genuine rookie displacer earns through the
    rookie projection path. Camp bodies and hallucinated names have no profile and so never
    appear. Feeds the STEP 4b substantiation guard in _apply_dependency_adjustment.
    """
    out: set = set()
    for p in players:
        profile = getattr(p, "profile", None)
        baseline = getattr(profile, "clean_season_baseline", None) if profile else None
        if not baseline:
            continue
        val = baseline.get("projected_ppr_season") or baseline.get("ppr_points") or 0
        try:
            if float(val or 0) > 0:
                out.add(_prod_key_full(p.name or ""))
        except (TypeError, ValueError):
            continue
    return out


def _apply_dependency_adjustment(
    ppr: float,
    dependencies: list,
    player_name: str | None = None,
    prior_production: dict | None = None,
    suppressed_log: list | None = None,
    credible_triggers: set | None = None,
    projection_prices_flags: bool = False,
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
            # Skip when the projection already priced it — see
            # _PROJECTION_PRICES_FLAGS_SOURCES. Applying it here too was a double-count.
            if projection_prices_flags:
                continue
            total_adj += impact
        elif flag == "displaced" and trigger == "active_and_healthy":
            # Same double-count in the negative direction, and the larger half of it:
            # 238 displaced rows in the DB against 63 beneficiary rows. The guards below
            # still run for the Python-projection players who reach them.
            if projection_prices_flags:
                continue
            trigger_name = dep.trigger_player_name
            # Only a real str name is keyable — anything else (None, a mock) leaves the
            # key empty, which makes BOTH guards inert for this flag rather than raising.
            trigger_key = _prod_key_full(trigger_name) if isinstance(trigger_name, str) else ""
            # STEP 4b substantiation guard — skip a NEGATIVE displaced adj whose trigger is
            # not an NFL contributor at all (no prior production AND no forward projection).
            # Runs BEFORE the direction guard: that one needs the trigger's production to
            # compare against, so it can never catch this case.
            if (
                impact < 0
                and credible_triggers is not None
                and trigger_key
                and trigger_key not in (prior_production or {})
                and trigger_key not in credible_triggers
            ):
                logger.warning(
                    "DISPLACED SUBSTANTIATION GUARD suppressed %+.0f%% on %s — trigger %r "
                    "has no prior production and no projection (not an NFL contributor)",
                    impact * 100, player_name, dep.trigger_player_name,
                )
                if suppressed_log is not None:
                    suppressed_log.append({
                        "player": player_name, "trigger": dep.trigger_player_name,
                        "player_ppg": None, "trigger_ppg": None,
                        "suppressed_pct": round(impact * 100, 0),
                        "reason": "unsubstantiated_trigger",
                    })
                continue  # trigger does not exist → do NOT apply the negative adj
            # STEP 4 direction guard — skip a NEGATIVE displaced adj when the flagged
            # player out-produced the trigger per-game last season (both real samples).
            if impact < 0 and prior_production and isinstance(player_name, str) and trigger_key:
                fp = prior_production.get(_prod_key_full(player_name))
                tp = prior_production.get(trigger_key)
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
                            "reason": "backwards_direction",
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
