"""Phase 2 (part 2) — the pre-draft display overlay helper.

Pure-logic tests for resolve_scoring_format + overlay_for (no DB). The overlay carries ONLY
non-$ per-format fields (tier + projected points + market ADP); PPR is a no-op passthrough so
callers stay byte-identical.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.services.format_display import (
    FormatOverlay, overlay_for, resolve_scoring_format,
)


class TestResolveScoringFormat:
    def test_supported_formats_pass_through(self):
        assert resolve_scoring_format("ppr") == ("ppr", False)
        assert resolve_scoring_format("half_ppr") == ("half_ppr", False)
        assert resolve_scoring_format("standard") == ("standard", False)

    def test_none_defaults_to_ppr_with_disclosure(self):
        assert resolve_scoring_format(None) == ("ppr", True)

    def test_unsupported_or_custom_defaults_to_ppr_with_disclosure(self):
        assert resolve_scoring_format("half") == ("ppr", True)      # not a preset key
        assert resolve_scoring_format("0.75ppr") == ("ppr", True)


def _row(tier=None, pts=None, adp=None, ai_bid_ceiling=None, value_assessment=None,
         auction_note=None, recommended_bid_ceiling=None, baseline_value=None):
    return SimpleNamespace(tier=tier, projected_points=pts, adp_fantasypros=adp,
                           ai_bid_ceiling=ai_bid_ceiling, value_assessment=value_assessment,
                           auction_note=auction_note, recommended_bid_ceiling=recommended_bid_ceiling,
                           baseline_value=baseline_value)


class TestOverlayFor:
    def test_ppr_is_noop_passthrough(self):
        ov = overlay_for("p1", {"p1": _row(tier=2, pts=200)}, "ppr")
        assert ov == FormatOverlay(None, None, None, False)  # PPR never overlays

    def test_non_ppr_with_row_overlays_tier_points_adp(self):
        ov = overlay_for("p1", {"p1": _row(tier=4, pts=126.4, adp=40)}, "standard")
        assert ov.tier == 4 and ov.projected_points == 126.4
        assert ov.adp_fantasypros == 40.0 and ov.adp_defaulted is False

    def test_non_ppr_missing_row_defaults_and_discloses(self):
        ov = overlay_for("ghost", {}, "standard")
        assert ov.tier is None and ov.projected_points is None
        assert ov.adp_fantasypros is None and ov.adp_defaulted is True

    def test_non_ppr_row_without_adp_discloses_adp_fallback(self):
        # Value repriced (tier/points present) but per-format market ADP not populated.
        ov = overlay_for("p1", {"p1": _row(tier=4, pts=126.4, adp=None)}, "half_ppr")
        assert ov.tier == 4 and ov.projected_points == 126.4
        assert ov.adp_fantasypros is None and ov.adp_defaulted is True
