"""Per-format DISPLAY overlay for PRE-DRAFT surfaces (draft board, /players, /teams).

THE TWO-BASIS RULE (Phase 2):
  * PRE-DRAFT surfaces (this module) read the per-format row from player_format_values —
    the pre-draft valuation repriced into the league's format.
  * IN-SEASON surfaces (trade, waiver) RE-SCORE live weekly production per format and must
    NOT use this module.

Overlaid per format: tier, projected points, the per-format FantasyPros ADP, the hybrid
auction $ (ai_bid_ceiling / tier-band ceiling+baseline), the market-blind prose — and the
MARKET COMPARISON (market_value + value_gap), which is what makes the board's market
column and GAP column move when the league's scoring format changes.

PPR is byte-identical: for "ppr" this module returns NO rows, so every caller keeps its
existing players-table values unchanged.

THE GAP INVARIANT. `value_gap` is a subtraction of two figures the user can see on the
same row — our bid ceiling minus the market's price. Whenever a caller shows a per-format
ceiling it must show the gap computed from THAT ceiling, or the row contradicts itself.
Before this, the ceiling was per-format while both the market column and the gap were
still PPR, so every non-PPR row printed a gap that matched neither of its own numbers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.player_format_values import PlayerFormatValues
from backend.scoring import DEFAULT_FORMAT, is_supported
# Imported, never restated: the ±$5 "aligned" band is defined once, in the valuation
# engine that sets the PPR signal. A second copy of these numbers here is how the two
# formats would silently drift apart.
from backend.engines.valuation import (
    VALUE_GAP_OVERVALUE_THRESHOLD,
    VALUE_GAP_UNDERVALUE_THRESHOLD,
)


def resolve_scoring_format(raw: Optional[str]) -> tuple[str, bool]:
    """(scoring_format, defaulted). A supported preset passes through; anything null/
    unsupported/custom → PPR + defaulted=True so the UI can disclose 'showing PPR'."""
    if is_supported(raw):
        return raw, False  # type: ignore[return-value]
    return DEFAULT_FORMAT, True


async def load_format_rows(
    session: AsyncSession,
    player_ids: Iterable[uuid.UUID],
    scoring_format: str,
) -> dict[str, PlayerFormatValues]:
    """Per-format PFV rows keyed by str(player_id). Empty for PPR (callers keep the
    players-table values → byte-identical) or when no ids are given."""
    ids = list(player_ids)
    if scoring_format == "ppr" or not ids:
        return {}
    rows = (await session.execute(
        select(PlayerFormatValues).where(
            PlayerFormatValues.player_id.in_(ids),
            PlayerFormatValues.scoring_format == scoring_format,
        )
    )).scalars().all()
    return {str(r.player_id): r for r in rows}


@dataclass(frozen=True)
class FormatOverlay:
    """The per-format overlay for one player. Fields are None when the PFV row is
    absent → the caller keeps its players-table (PPR) value for that field."""
    tier: Optional[int]
    projected_points: Optional[float]
    adp_fantasypros: Optional[float]
    adp_defaulted: bool   # True → no per-format market ADP; the shown ADP is still PPR
    # Hybrid auction $ + reasoning (the market-blind opinion; non-PPR only). None → PPR
    # passthrough. These are what make a non-PPR user see their own $ and prose, not PPR's.
    ai_bid_ceiling: Optional[int] = None
    value_assessment: Optional[str] = None
    auction_note: Optional[str] = None
    # The tier-band $ (anchor) so the board's ceiling/system columns aren't a PPR mix.
    recommended_bid_ceiling: Optional[float] = None
    baseline_value: Optional[float] = None
    # Per-format MARKET auction $ (PFV.auction_value — the same FantasyPros DraftWizard
    # feed that fills players.market_value_fantasypros for PPR, scraped for this format).
    market_value: Optional[float] = None
    market_defaulted: bool = False  # True → no per-format market $; shown market is PPR


def _market_relative(gap: Optional[float]) -> Optional[str]:
    """value_gap_signal for a NON-PPR row, from that row's own dollar gap.

    PPR's signal comes from the price-curve conviction on the players table and is NOT
    touched here. No per-format price curve exists (there is no per-format
    signal_conviction column), so a non-PPR row derives its signal from the same dollar
    gap it displays, using the shared aligned band. That guarantees the number and the
    chip cannot disagree in sign, which is the whole point of computing it here rather
    than letting a PPR signal sit next to a non-PPR gap.
    """
    if gap is None:
        return None
    if gap < float(VALUE_GAP_OVERVALUE_THRESHOLD):
        return "market_overvalues"
    if gap > float(VALUE_GAP_UNDERVALUE_THRESHOLD):
        return "market_undervalues"
    return "aligned"


def compute_format_gap(
    ceiling: Optional[float], market: Optional[float],
) -> tuple[Optional[float], Optional[str]]:
    """(value_gap, value_gap_signal) for a non-PPR row: ceiling − market, rounded to cents.

    Same definition as ``engines.valuation.compute_value_gap_from_player`` — what we would
    bid minus what he will cost — applied to the pair of numbers the row actually shows.
    Returns (None, None) when either side is missing, so the surface renders "--" rather
    than a gap against a price from a different scoring format.
    """
    if ceiling is None or market is None:
        return None, None
    gap = round(float(ceiling) - float(market), 2)
    return gap, _market_relative(gap)


def overlay_for(
    player_id: str, fmt_rows: dict[str, PlayerFormatValues], scoring_format: str,
) -> FormatOverlay:
    """Resolve the overlay for a player. PPR (empty fmt_rows) → all-None passthrough +
    adp_defaulted=False. Non-PPR → PFV tier/points/$ /prose, per-format ADP and market $
    when populated (else the *_defaulted flags tell the surface it is showing PPR)."""
    if scoring_format == "ppr":
        return FormatOverlay(None, None, None, False)
    row = fmt_rows.get(player_id)
    if row is None:
        return FormatOverlay(None, None, None, True, market_defaulted=True)
    return FormatOverlay(
        tier=row.tier,
        projected_points=float(row.projected_points) if row.projected_points is not None else None,
        adp_fantasypros=float(row.adp_fantasypros) if row.adp_fantasypros is not None else None,
        adp_defaulted=row.adp_fantasypros is None,
        ai_bid_ceiling=int(row.ai_bid_ceiling) if row.ai_bid_ceiling is not None else None,
        value_assessment=row.value_assessment,
        auction_note=row.auction_note,
        recommended_bid_ceiling=float(row.recommended_bid_ceiling) if row.recommended_bid_ceiling is not None else None,
        baseline_value=float(row.baseline_value) if row.baseline_value is not None else None,
        market_value=float(row.auction_value) if row.auction_value is not None else None,
        market_defaulted=row.auction_value is None,
    )


def resolve_market_and_gap(
    overlay: Optional[FormatOverlay],
    ppr_market: Optional[float],
    ppr_gap: Optional[float],
    ppr_gap_signal: Optional[str],
    effective_ceiling: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """(market_value, value_gap, value_gap_signal) for one row, on ONE consistent basis.

    PPR (overlay None / all-None) returns the players-table trio unchanged — byte-identical.

    Non-PPR returns the per-format market $ and a gap recomputed from ``effective_ceiling``
    (the ceiling the caller is about to display). When the per-format market $ has not been
    scraped yet, the PPR market $ is shown as a fallback and the gap is STILL recomputed
    against it, so the printed gap always equals the two numbers printed beside it. The
    stale players-table ``value_gap`` is never returned for a non-PPR row.
    """
    if overlay is None or (overlay.market_value is None and not overlay.market_defaulted):
        return ppr_market, ppr_gap, ppr_gap_signal
    market = overlay.market_value if overlay.market_value is not None else ppr_market
    gap, signal = compute_format_gap(effective_ceiling, market)
    return market, gap, signal
