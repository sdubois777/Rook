"""
Standard K / DST fantasy scoring (K/DEF streaming arc — slice 2).

Applies STANDARD scoring to the RAW weekly K/DST stat lines from slice 1
(nfl_weekly.weekly_kdef_usage) to get weekly fantasy points, then shapes them for
the in-season value engine (compute_player_value / evaluate_league) — the same
per-week-fantasy-points basis the skill positions use.

SCORING is intentionally isolated in ONE constants block: DST scoring — especially
the points-allowed tiers — is heavily league-variable, so a future league-scoring
slice swaps these constants without touching the data layer or the value engine.
Convention chosen this slice: points_allowed is the opponent's FINAL score (as
stored in slice 1); the stored opp_nonoffense_* components stay UNUSED here.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# STANDARD scoring constants — the ONE place a league-scoring slice retunes.
# ---------------------------------------------------------------------------
# DST per-event.
DST_SACK = 1.0
DST_INT = 2.0
DST_FUMBLE_REC = 2.0
DST_SAFETY = 2.0
DST_DEF_ST_TD = 6.0
# DST points-allowed tiers: (max points-allowed INCLUSIVE, fantasy points). Standard
# breakpoints, sliding from a shutout bonus to a 35+ penalty.
DST_PA_TIERS: tuple[tuple[float, float], ...] = (
    (0, 10.0),
    (6, 7.0),
    (13, 4.0),
    (20, 1.0),
    (27, 0.0),
    (34, -1.0),
    (float("inf"), -4.0),
)

# K — distance-banded FG (this is why slice 1 kept unbucketed per-kick distances).
K_XP_MADE = 1.0
K_XP_MISS = -1.0
K_FG_MISS = -1.0            # applied to each missed/blocked FG
# FG made bands: (max distance INCLUSIVE, points).
K_FG_BANDS: tuple[tuple[float, float], ...] = (
    (39, 3.0),
    (49, 4.0),
    (float("inf"), 5.0),
)


def dst_points_allowed_score(points_allowed: float) -> float:
    """Tiered points-allowed fantasy points (the first tier the PA falls into)."""
    pa = float(points_allowed)
    for max_pa, pts in DST_PA_TIERS:
        if pa <= max_pa:
            return pts
    return DST_PA_TIERS[-1][1]


def fg_distance_score(distance: float) -> float:
    """Fantasy points for one MADE field goal by distance band."""
    d = float(distance)
    for max_dist, pts in K_FG_BANDS:
        if d <= max_dist:
            return pts
    return K_FG_BANDS[-1][1]


def score_dst_line(row) -> float:
    """Standard fantasy points for one weekly DST raw line."""
    return round(
        float(row["sacks"]) * DST_SACK
        + float(row["interceptions"]) * DST_INT
        + float(row["fumble_recoveries"]) * DST_FUMBLE_REC
        + float(row["safeties"]) * DST_SAFETY
        + float(row["def_st_tds"]) * DST_DEF_ST_TD
        + dst_points_allowed_score(row["points_allowed"]),
        2,
    )


def score_k_line(row) -> float:
    """Standard fantasy points for one weekly kicker raw line — made FGs by
    distance band, XP, and per-miss/block penalties."""
    distances = row["fg_made_distances"]
    if distances is None or (isinstance(distances, float) and pd.isna(distances)):
        distances = []
    fg_pts = sum(fg_distance_score(d) for d in distances)
    misses = float(row.get("fg_missed", 0)) + float(row.get("fg_blocked", 0))
    xp_misses = float(row.get("xp_missed", 0)) + float(row.get("xp_blocked", 0))
    return round(
        fg_pts
        + float(row.get("xp_made", 0)) * K_XP_MADE
        + misses * K_FG_MISS
        + xp_misses * K_XP_MISS,
        2,
    )


def score_weekly_kdef(kdef: pd.DataFrame) -> pd.DataFrame:
    """Add a ``fantasy_points`` column to the raw weekly K/DST frame
    (weekly_kdef_usage output). Position-typed: 'DEF' → DST scoring, 'K' → K
    scoring. Loud-warns any row with an unrecognized position (kept, points 0)."""
    if kdef is None or kdef.empty:
        return kdef.assign(fantasy_points=[]) if kdef is not None else kdef

    def _score(row):
        pos = row["position"]
        if pos == "DEF":
            return score_dst_line(row)
        if pos == "K":
            return score_k_line(row)
        logger.warning("score_weekly_kdef: unrecognized position %r (points=0)", pos)
        return 0.0

    out = kdef.copy()
    out["fantasy_points"] = out.apply(_score, axis=1)
    return out


# Usage columns the value engine reads but which are meaningless for K/DEF — set to
# zero so the trajectory / opportunity-gap factors neutralize (opp_gap_factor's
# min-expected guard makes zero volume a no-op), leaving value = the scoring LEVEL.
_ZERO_USAGE = ("snap_pct", "target_share", "targets", "carries")


def kdef_value_frame(scored: pd.DataFrame) -> pd.DataFrame:
    """Shape scored K/DST rows into the value engine's per-week schema, so
    compute_player_value / evaluate_league consume them exactly like skill rows.
    ``fantasy_points_ppr`` carries the K/DST score (the column the engine sums);
    usage columns are zero (no snaps/targets for K/DEF)."""
    if scored is None or scored.empty:
        return pd.DataFrame(columns=[
            "canonical_player_id", "player_name", "position", "nfl_team",
            "season", "week", "fantasy_points_ppr", *_ZERO_USAGE,
        ])
    out = scored.copy()
    out["fantasy_points_ppr"] = out["fantasy_points"]
    for col in _ZERO_USAGE:
        out[col] = 0.0
    keep = ["canonical_player_id", "player_name", "position", "nfl_team",
            "season", "week", "fantasy_points_ppr", *_ZERO_USAGE]
    return out[[c for c in keep if c in out.columns]].reset_index(drop=True)


async def weekly_kdef_value_frame(
    season: int,
    weeks: Optional[Iterable[int]] = None,
    db=None,
) -> pd.DataFrame:
    """Value-engine-ready K/DST weekly frame: slice-1 raw lines → standard scoring
    → engine schema. Slice 3 unions this into the weekly frame evaluate_league
    receives; the offense frame is untouched (additive rows)."""
    from backend.integrations.nfl_weekly import weekly_kdef_usage

    raw = await weekly_kdef_usage(season, weeks=weeks, db=db)
    return kdef_value_frame(score_weekly_kdef(raw))
