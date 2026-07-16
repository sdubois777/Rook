"""Per-format DISPLAY overlay for PRE-DRAFT surfaces (draft board, /players, /teams).

THE TWO-BASIS RULE (Phase 2):
  * PRE-DRAFT surfaces (this module) read the per-format row from player_format_values —
    the pre-draft valuation repriced into the league's format.
  * IN-SEASON surfaces (trade, waiver) RE-SCORE live weekly production per format and must
    NOT use this module.

Only the NON-DOLLAR per-format fields are overlaid: tier + projected points (and the
per-format FantasyPros ADP where a pipeline run has populated it). Auction DOLLAR figures
(baseline_value-as-$, recommended/ai_bid_ceiling, market_value, value_gap) stay on the
players-table (PPR) path — the within-position auction-$ divergence is an unresolved
blocker, so $ stays dark until that separate build.

PPR is byte-identical: for "ppr" this module returns NO rows, so every caller keeps its
existing players-table values unchanged.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.player_format_values import PlayerFormatValues
from backend.scoring import DEFAULT_FORMAT, is_supported


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
    """The non-$ per-format overlay for one player. Fields are None when the PFV row is
    absent → the caller keeps its players-table (PPR) value for that field."""
    tier: Optional[int]
    projected_points: Optional[float]
    adp_fantasypros: Optional[float]
    adp_defaulted: bool   # True → no per-format market ADP; the shown ADP is still PPR


def overlay_for(
    player_id: str, fmt_rows: dict[str, PlayerFormatValues], scoring_format: str,
) -> FormatOverlay:
    """Resolve the overlay for a player. PPR (empty fmt_rows) → all-None passthrough +
    adp_defaulted=False. Non-PPR → PFV tier/points, and per-format ADP when populated
    (else adp_defaulted=True so the surface discloses the PPR ADP fallback)."""
    if scoring_format == "ppr":
        return FormatOverlay(None, None, None, False)
    row = fmt_rows.get(player_id)
    if row is None:
        return FormatOverlay(None, None, None, True)
    return FormatOverlay(
        tier=row.tier,
        projected_points=float(row.projected_points) if row.projected_points is not None else None,
        adp_fantasypros=float(row.adp_fantasypros) if row.adp_fantasypros is not None else None,
        adp_defaulted=row.adp_fantasypros is None,
    )
