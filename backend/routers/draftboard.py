"""
Draft board router — tiered player rankings with strategy highlighting.

Endpoints:
  GET /draftboard  — all ranked players grouped by tier with strategy mode
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.agents.valuation_agent import classify_snake_flag, compute_adp_diff
from backend.core.dependencies import get_current_user
from backend.database import AsyncSessionLocal
from backend.models.player import Player, PlayerProfile
from backend.models.dependency import PlayerDependency
from backend.models.league_config import DEFAULT_LEAGUE_CONFIG
# The strategy vocabulary is validated on write in the preferences router; the board reads
# the same set so a strategy the user can SAVE is always one the board can HIGHLIGHT.
from backend.routers.preferences import VALID_STRATEGIES
from backend.schemas.player_badges import PlayerBadgeFields
from backend.repositories.player_repo import draftable_filter
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/draftboard", tags=["draftboard"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class DraftBoardFlag(BaseModel):
    flag_type: str
    trigger_player_name: Optional[str] = None
    confidence: Optional[str] = None


class DraftBoardPlayer(PlayerBadgeFields):
    id: str
    name: str
    team_abbr: Optional[str] = None
    position: Optional[str] = None
    tier: Optional[int] = None
    recommended_bid_ceiling: Optional[float] = None
    baseline_value: Optional[float] = None
    market_value: Optional[float] = None
    market_value_season: Optional[int] = None
    prior_season_price: Optional[float] = None
    prior_season_year: Optional[int] = None
    value_gap: Optional[float] = None
    value_gap_signal: Optional[str] = None
    breakout_flag: bool = False
    is_rookie: bool = False
    ppr_points: Optional[float] = None
    injury_risk_level: Optional[str] = None
    ai_bid_ceiling: Optional[int] = None
    pay_up_flag: bool = False
    nomination_target_flag: bool = False
    value_assessment: Optional[str] = None
    # Pre-draft availability discount (engines/availability.py): the value fields above
    # (recommended/ai_bid_ceiling/ppr_points) are ALREADY discounted by this factor for
    # a known multi-week absence; these expose it for the UI (badge/warning).
    availability_factor: float = 1.0
    availability_games_missed: int = 0
    # Snake-draft ADP (null until a pipeline run populates them — UI shows "--")
    adp_ai: Optional[float] = None
    adp_fantasypros: Optional[float] = None
    adp_scoring: Optional[str] = None
    adp_rank: Optional[int] = None
    adp_diff: Optional[float] = None
    snake_flag: Optional[str] = None
    round_num: Optional[int] = None  # (adp_rank-1)//team_count + 1
    flags: list[DraftBoardFlag] = []
    strategy_highlight: Optional[str] = None  # "primary" / "secondary" / "dimmed" / None


class DraftBoardResponse(BaseModel):
    tiers: dict[str, list[DraftBoardPlayer]]
    strategy: Optional[str] = None
    total_players: int = 0
    # Phase 2: the format the tier/points were read in + disclosure when a non-PPR league
    # is defaulted to PPR (unsupported/custom) or is seeing PPR ADP (per-format ADP not
    # yet populated). Auction $ figures stay on the PPR path regardless (dark).
    scoring_format: str = "ppr"
    scoring_format_defaulted: bool = False
    adp_format_defaulted: bool = False
    # True → at least one row's MARKET auction $ is still the PPR figure because the
    # per-format DraftWizard $ has not been scraped yet. The gap is still recomputed
    # against whatever market $ is shown, so the row stays self-consistent; this flag
    # only says the market side of that subtraction is PPR.
    market_format_defaulted: bool = False


# ---------------------------------------------------------------------------
# Strategy highlighting logic
# ---------------------------------------------------------------------------
#
# WHY THIS IS BUILT ON PRICE, NOT ON `tier`.
#
# Every one of these strategies is a statement about how to spend a fixed auction budget
# (the definitions are `_STRATEGY_LABELS` in backend/engines/league_auction.py, quoted on
# each branch below). `tier`, however, is a WITHIN-POSITION z-score tier — see
# `compute_pool_ztiers` in backend/engines/valuation.py, which tiers each position against
# its own pool. So "tier 1" means "top of his own position", not "expensive", and the four
# strategies previously keyed off it produced answers that did not match their own names:
#
#   * stars_and_scrubs marked every tier-1 player a star, including tier-1 kickers and
#     defenses, and every tier-4-or-5 player a scrub — roughly half the board. It also
#     raised TypeError on any player whose tier was NULL (`tier >= 4` against None), which
#     fails the WHOLE request, not one row.
#   * zero_rb dimmed EVERY running back, including the $1-3 late-round backs the strategy
#     exists to draft.
#   * hero_rb highlighted every tier-1 RB, when the whole idea is one (at most two) of them.
#   * balanced returned None for everything — selecting it did nothing at all, while the
#     legend below the board still claimed there were primary and secondary targets.
#
# The replacement bands players by OUR OWN auction ceiling, which is the quantity these
# strategies are actually about, and ranks them within position so "one elite RB" can mean
# one.

# Band edges as FRACTIONS of the league auction budget, so a league with a different budget
# scales with them instead of inheriting $200 literals. At the default $200 budget these
# are >= $40 / $15-39 / $5-14 / < $5, which lines up with the TYPICAL_BID_RANGES table in
# docs/rules/LEAGUE_RULES.md: a stud (WR1 $40-60, RB1 $50-75), a starter (WR2 $15-30, RB2
# $20-40), a flex/bench piece (FLEX $10-25), and a near-minimum filler.
_BAND_ELITE_PCT = 0.20
_BAND_MID_PCT = 0.075
_BAND_VALUE_PCT = 0.025

BAND_ELITE = "elite"
BAND_MID = "mid"
BAND_VALUE = "value"
BAND_CHEAP = "cheap"

# K/DEF are $1 streamers BY DESIGN (valuation.py pins them to tier 5 at $1). They would
# land in the cheap band en masse and light up under any "fill cheap" strategy, which
# tells the user nothing — you draft one kicker and one defense last whatever you are
# running. They are never highlighted, in any strategy.
_STRATEGY_EXEMPT_POSITIONS = frozenset({"K", "DEF", "DST"})

# hero_rb = "Pays premium for 1-2 elite RBs". A third premium RB is a different (and
# unaffordable) plan, so only the top two RBs by our own price are hero candidates.
_HERO_RB_SLOTS = 2

# zero_rb = "invests in WR/TE/QB" — the positions the money goes to instead of RB.
_ZERO_RB_INVEST_POSITIONS = frozenset({"WR", "TE", "QB"})


def price_band(ceiling: Optional[float], budget: int = DEFAULT_LEAGUE_CONFIG.budget) -> Optional[str]:
    """Which spending band a player sits in, from our own auction ceiling.

    Returns None when we have no price for him — an unpriced player gets no strategy
    verdict rather than being silently swept into the cheap band.
    """
    if ceiling is None:
        return None
    c = float(ceiling)
    if c >= budget * _BAND_ELITE_PCT:
        return BAND_ELITE
    if c >= budget * _BAND_MID_PCT:
        return BAND_MID
    if c >= budget * _BAND_VALUE_PCT:
        return BAND_VALUE
    return BAND_CHEAP


def build_positional_ranks(players: list[DraftBoardPlayer]) -> dict[str, int]:
    """{player id: 1-based rank within his position, by our own ceiling descending}.

    Ranked on the ceiling the board is about to DISPLAY, so in a non-PPR league the "top
    two RBs" are that format's top two, not PPR's. Unpriced players rank last.
    """
    ranks: dict[str, int] = {}
    by_pos: dict[str, list[DraftBoardPlayer]] = {}
    for p in players:
        by_pos.setdefault(p.position or "", []).append(p)
    for group in by_pos.values():
        ordered = sorted(
            group,
            key=lambda p: (p.ai_bid_ceiling if p.ai_bid_ceiling is not None else -1.0),
            reverse=True,
        )
        for i, p in enumerate(ordered, start=1):
            ranks[p.id] = i
    return ranks


def _apply_strategy(
    player: DraftBoardPlayer,
    strategy: str,
    band: Optional[str] = None,
    pos_rank: Optional[int] = None,
) -> str | None:
    """The highlight for one player under one strategy: primary / secondary / dimmed / None.

    primary   = spend here, this is what the strategy is buying
    secondary = the supporting picks the strategy fills around it
    dimmed    = deliberately skipped by this strategy
    None      = the strategy has no opinion
    """
    pos = (player.position or "").upper()
    if pos in _STRATEGY_EXEMPT_POSITIONS:
        return None
    if band is None:
        band = price_band(player.ai_bid_ceiling)
    if band is None:
        return None  # no price → no opinion, never a guess

    # "Pays premium for 1-2 elite RBs, cheap elsewhere."
    if strategy == "hero_rb":
        if pos == "RB":
            if band == BAND_ELITE:
                return "primary" if (pos_rank or 99) <= _HERO_RB_SLOTS else "dimmed"
            # The mid band is the RB dead zone: too expensive to be a value pick, not
            # good enough to be the hero. This strategy exists to skip it.
            return "dimmed" if band == BAND_MID else "secondary"
        # "Cheap elsewhere" — the money is committed to the hero, so another elite
        # price tag competes with him; the cheap fills are the plan.
        if band == BAND_ELITE:
            return "dimmed"
        if band in (BAND_VALUE, BAND_CHEAP):
            return "secondary"
        return None

    # "Avoids expensive RBs, invests in WR/TE/QB."
    if strategy == "zero_rb":
        if pos == "RB":
            # The late cheap backs ARE the strategy — zero RB means zero EARLY RB, not
            # zero RB. Dimming them (the old behaviour) inverted the advice.
            return "dimmed" if band in (BAND_ELITE, BAND_MID) else "secondary"
        if pos in _ZERO_RB_INVEST_POSITIONS and band in (BAND_ELITE, BAND_MID):
            return "primary"
        return None

    # "Spends big on 2-3 studs, fills rest at $1."
    if strategy == "stars_and_scrubs":
        if band == BAND_ELITE:
            return "primary"
        if band == BAND_CHEAP:
            return "secondary"
        if band == BAND_MID:
            return "dimmed"
        return None

    # "Spreads budget relatively evenly across positions." One stud eats the spread, and
    # $1 filler leaves money unspent — this strategy lives in the middle of the board.
    if strategy == "balanced":
        if band == BAND_MID:
            return "primary"
        if band == BAND_VALUE:
            return "secondary"
        if band == BAND_ELITE:
            return "dimmed"
        return None

    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_SNAKE_TEAM_COUNT = 12  # rounds = (adp_rank - 1) // team_count + 1


@router.get("", response_model=DraftBoardResponse)
async def get_draftboard(
    position: Optional[str] = None,
    tier: Optional[int] = None,
    strategy: Optional[str] = None,
    scoring_format: str = "ppr",
    draft_type: str = "auction",
    _user=Depends(get_current_user),
):
    """Ranked players. Auction: grouped by tier, sorted by bid ceiling. Snake:
    grouped by round, sorted by adp_rank (only players with an ADP rank).

    PRE-DRAFT surface → per-format TIER + projected POINTS read from player_format_values
    (PPR byte-identical). Auction $ stays dark; per-format ADP is read where a pipeline run
    has populated it, else PPR + disclosure."""
    from backend.services.format_display import (
        load_format_rows, overlay_for, resolve_market_and_gap, resolve_scoring_format,
    )
    is_snake = draft_type == "snake"
    scoring_format, fmt_defaulted = resolve_scoring_format(scoring_format)

    async with AsyncSessionLocal() as session:
        query = (
            select(Player)
            # Snake needs a computed ADP rank; auction needs a bid ceiling.
            .where(
                Player.adp_rank.isnot(None)
                if is_snake
                else Player.recommended_bid_ceiling.isnot(None)
            )
            .where(draftable_filter())
            .options(
                selectinload(Player.dependencies),
                selectinload(Player.injury_profile),
                selectinload(Player.profile),
                selectinload(Player.historic_prices),
            )
        )

        if position:
            query = query.where(Player.position == position.upper())
        if tier is not None:
            query = query.where(Player.tier == tier)

        if is_snake:
            query = query.order_by(Player.adp_rank.asc().nulls_last())
        else:
            query = query.order_by(
                Player.tier.asc().nulls_last(),
                Player.recommended_bid_ceiling.desc().nulls_last(),
            )

        result = await session.execute(query)
        players = result.scalars().all()

        # Per-format overlay rows (empty for PPR → byte-identical).
        fmt_rows = await load_format_rows(session, [p.id for p in players], scoring_format)

    # Build response grouped by tier
    tiers: dict[str, list[DraftBoardPlayer]] = {}
    built: list[DraftBoardPlayer] = []   # same objects, flat — for the strategy pass below
    total = 0
    adp_format_defaulted = False
    market_format_defaulted = False

    prior_year = get_current_season() - 1

    for p in players:
        flags = []
        for dep in (p.dependencies or []):
            flags.append(DraftBoardFlag(
                flag_type=dep.flag_type,
                trigger_player_name=dep.trigger_player_name,
                confidence=dep.confidence,
            ))

        # Look up prior season price from historic table
        hist_price = None
        for hp in (p.historic_prices or []):
            if hp.season_year == prior_year:
                hist_price = float(hp.price)
                break

        # Pre-draft availability discount — the DRAFT-RANKED value is base × factor
        # (deterministic games-missed proration for a known multi-week absence). Base
        # columns are untouched in the DB; discounted here at read time (idempotent).
        avf = float(p.availability_factor) if p.availability_factor is not None else 1.0
        _raw_proj = (
            p.profile.clean_season_baseline.get("projected_ppr_season")
            or p.profile.clean_season_baseline.get("ppr_points")
        ) if (p.profile and p.profile.clean_season_baseline
              and (p.profile.clean_season_baseline.get("projected_ppr_season") is not None
                   or p.profile.clean_season_baseline.get("ppr_points") is not None)) else None

        # Per-format overlay: tier + points reprice by format; and (non-PPR) the hybrid
        # auction $ + market-blind reasoning + tier-band ceiling/baseline. Each field
        # falls back to the players-table (PPR) value when the overlay has none — so PPR
        # is byte-identical and a non-PPR user sees their own $ and prose.
        ov = overlay_for(str(p.id), fmt_rows, scoring_format)
        eff_tier = ov.tier if ov.tier is not None else p.tier
        if ov.projected_points is not None:
            _raw_proj = ov.projected_points   # already the format's SEASON total
        elif p.adjusted_points is not None:
            # PPR only (non-PPR always has an overlay above). Show the points the DOLLARS
            # were computed from, not the raw projection. Without this the column is raw
            # while recommended_bid_ceiling/ai_bid_ceiling derive from raw x injury x
            # dependency, so a player projected fewer points can be priced higher — 146
            # inverted WR pairs in the top 40 alone. ppr_to_system_value is affine in this
            # quantity, so displaying it makes the board monotone by construction.
            _raw_proj = float(p.adjusted_points)
        if scoring_format != "ppr" and ov.adp_defaulted:
            adp_format_defaulted = True
        eff_ai_ceiling = ov.ai_bid_ceiling if ov.ai_bid_ceiling is not None else p.ai_bid_ceiling
        eff_rec_ceiling = ov.recommended_bid_ceiling if ov.recommended_bid_ceiling is not None else (
            float(p.recommended_bid_ceiling) if p.recommended_bid_ceiling else None)
        eff_baseline = ov.baseline_value if ov.baseline_value is not None else (
            float(p.baseline_value) if p.baseline_value else None)
        eff_value_assessment = ov.value_assessment if ov.value_assessment is not None else p.value_assessment

        # MARKET + GAP on the SAME basis as the ceiling above. PPR passes the players-table
        # trio straight through (byte-identical). Non-PPR reads the format's own market $
        # and recomputes the gap from eff_ai_ceiling, so the GAP column is always the
        # subtraction of the two numbers printed next to it. Previously the ceiling moved
        # with the format while the market column and the gap stayed on PPR, which is the
        # reported "GAP is wrong on everything except PPR".
        eff_market, eff_gap, eff_gap_signal = resolve_market_and_gap(
            ov,
            float(p.market_value_fantasypros) if p.market_value_fantasypros else None,
            float(p.value_gap) if p.value_gap is not None else None,
            p.value_gap_signal,
            eff_ai_ceiling,
        )
        if scoring_format != "ppr" and ov.market_defaulted:
            market_format_defaulted = True

        # SNAKE columns, same invariant as GAP above. Diff is defined as (FP rank − our
        # rank), so once the FP rank shown is the format's own, the stored PPR diff is a
        # subtraction of two numbers that are no longer both on screen. Recompute it — and
        # the badge derived from it — against the FP rank actually displayed. Our own rank
        # (adp_rank) stays PPR: no per-format AI ordering exists, which adp_format_defaulted
        # already discloses. When the overlay has no per-format ADP the stored PPR values
        # are already correct for the PPR figure being shown, so they pass through.
        eff_adp_fp = (
            ov.adp_fantasypros if ov.adp_fantasypros is not None
            else (float(p.adp_fantasypros) if p.adp_fantasypros is not None else None)
        )
        eff_adp_diff = float(p.adp_diff) if p.adp_diff is not None else None
        eff_snake_flag = p.snake_flag
        if ov.adp_fantasypros is not None:
            eff_adp_diff = compute_adp_diff(eff_adp_fp, p.adp_rank)
            eff_snake_flag = classify_snake_flag(
                eff_adp_diff, eff_tier, adp_rank=p.adp_rank, fp_rank=eff_adp_fp,
            )

        dbp = DraftBoardPlayer(
            id=str(p.id),
            name=p.name,
            team_abbr=p.team_abbr,
            position=p.position,
            tier=eff_tier,
            recommended_bid_ceiling=round(eff_rec_ceiling * avf, 1) if eff_rec_ceiling else None,
            baseline_value=eff_baseline,
            market_value=eff_market,
            market_value_season=get_current_season() if eff_market is not None else None,
            prior_season_price=hist_price,
            prior_season_year=prior_year if hist_price else None,
            # `is not None`, not truthiness: an exactly-zero gap is a real answer
            # ("we agree with the market to the dollar"), not missing data. The board
            # now renders this field directly instead of recomputing it, so a 0 that
            # serialises as null shows "--" where it should show "0".
            value_gap=eff_gap,
            value_gap_signal=eff_gap_signal,
            ppr_points=round(float(_raw_proj) * avf, 1) if _raw_proj is not None else None,
            breakout_flag=p.breakout_flag or False,
            is_rookie=p.is_rookie or False,
            injury_status=p.injury_status,
            injury_risk_level=p.injury_profile.overall_risk_level if p.injury_profile else None,
            availability_factor=avf,
            availability_games_missed=p.availability_games_missed or 0,
            ai_bid_ceiling=round(eff_ai_ceiling * avf) if eff_ai_ceiling else eff_ai_ceiling,
            pay_up_flag=p.pay_up_flag or False,
            nomination_target_flag=p.nomination_target_flag or False,
            value_assessment=eff_value_assessment,
            adp_ai=float(p.adp_ai) if p.adp_ai is not None else None,
            # Per-format market ADP where a pipeline run has populated it; else the
            # players-table PPR value (adp_format_defaulted flags the fallback).
            adp_fantasypros=eff_adp_fp,
            adp_scoring=scoring_format if ov.adp_fantasypros is not None else p.adp_scoring,
            adp_rank=p.adp_rank,
            adp_diff=eff_adp_diff,
            snake_flag=eff_snake_flag,
            round_num=(p.adp_rank - 1) // _SNAKE_TEAM_COUNT + 1 if p.adp_rank else None,
            flags=flags,
            strategy_highlight=None,
        )

        # Snake groups by round; auction groups by the (per-format) tier.
        group_key = str(dbp.round_num or 0) if is_snake else str(eff_tier or 0)
        if group_key not in tiers:
            tiers[group_key] = []
        tiers[group_key].append(dbp)
        built.append(dbp)
        total += 1

    # STRATEGY PASS — runs after the whole board exists, because "the top two RBs" and
    # "the elite price band" are properties of the board, not of a row read in isolation.
    # `tiers` holds these same objects, so setting the highlight here is visible in the
    # response. Applies to snake as well as auction: hero RB / zero RB / stars-and-scrubs
    # are allocation plans, and the band comes from our own valuation, which both boards
    # carry.
    if strategy in VALID_STRATEGIES:
        pos_ranks = build_positional_ranks(built)
        for dbp in built:
            dbp.strategy_highlight = _apply_strategy(
                dbp, strategy,
                band=price_band(dbp.ai_bid_ceiling),
                pos_rank=pos_ranks.get(dbp.id),
            )

    return DraftBoardResponse(
        tiers=tiers,
        strategy=strategy,
        total_players=total,
        scoring_format=scoring_format,
        scoring_format_defaulted=fmt_defaulted,
        adp_format_defaulted=adp_format_defaulted,
        market_format_defaulted=market_format_defaulted,
    )
