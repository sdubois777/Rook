"""THE single scoring definition — one source of truth for PPR / Half / Standard.

Every per-format number in the system (points, VOR, replacement, value, ADP labels,
auction) derives from THIS module. Do not re-encode a reception multiplier anywhere
else: this codebase has repeatedly shipped the same constant in two places that then
disagreed (e.g. ``LeagueConfig.rec_points`` silently returned 1.0 for Standard). If
you need "points per reception," import it from here.

Reception-based reprice is EXACT across the three presets because yards and TDs score
identically in ppr/half/standard — only the per-reception bonus differs. So:

    points_in(fmt) = ppr_points − (1.0 − REC_POINTS[fmt]) × receptions

Custom scoring is OUT of scope: a custom league maps to the nearest preset via
``nearest_preset`` and the read layer discloses the approximation to the user.
"""
from __future__ import annotations

# The three supported presets, in descending reception value. Order matters for a
# few callers that iterate formats deterministically (e.g. per-format storage rows).
SCORING_FORMATS: tuple[str, ...] = ("ppr", "half_ppr", "standard")

# Points per reception — the ONLY thing that differs between these formats.
REC_POINTS: dict[str, float] = {
    "ppr": 1.0,
    "half_ppr": 0.5,
    "standard": 0.0,
}

DEFAULT_FORMAT = "ppr"


def rec_points(scoring_format: str | None) -> float:
    """Points per reception for a format. Unknown/None → PPR (the safe default —
    the read layer is responsible for DISCLOSING that it defaulted)."""
    return REC_POINTS.get(scoring_format or DEFAULT_FORMAT, REC_POINTS["ppr"])


def is_supported(scoring_format: str | None) -> bool:
    return scoring_format in REC_POINTS


def season_points(ppr_points: float, receptions: float, scoring_format: str) -> float:
    """Reprice a PPR season total into another format's total.

    ``ppr_points`` is a PPR total (it already includes 1.0 × receptions); this backs
    out the reception delta for the target format. Exact for ppr/half/standard because
    yards + TDs are format-invariant. ``receptions`` of 0 (QB/K/DEF, or a player whose
    reception count is unknown) → no change, which is correct for non-receivers and
    the honest fallback for unknowns.
    """
    if ppr_points is None:
        return 0.0
    rec = receptions or 0.0
    delta = (1.0 - rec_points(scoring_format)) * rec
    return round(max(0.0, float(ppr_points) - delta), 2)


def nearest_preset(points_per_reception: float | None) -> str:
    """Map an arbitrary custom reception value to the nearest supported preset.
    Used only to APPROXIMATE a custom league (with a user-facing disclosure)."""
    if points_per_reception is None:
        return DEFAULT_FORMAT
    return min(REC_POINTS, key=lambda f: abs(REC_POINTS[f] - float(points_per_reception)))
