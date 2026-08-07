"""Phase 2 (part 2) — the pre-draft display overlay helper.

Pure-logic tests for resolve_scoring_format + overlay_for + the market/gap resolution
(no DB). PPR is a no-op passthrough so callers stay byte-identical.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.services.format_display import (
    FormatOverlay, compute_format_gap, overlay_for, resolve_market_and_gap,
    resolve_scoring_format,
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
         auction_note=None, recommended_bid_ceiling=None, baseline_value=None,
         auction_value=None):
    return SimpleNamespace(tier=tier, projected_points=pts, adp_fantasypros=adp,
                           ai_bid_ceiling=ai_bid_ceiling, value_assessment=value_assessment,
                           auction_note=auction_note, recommended_bid_ceiling=recommended_bid_ceiling,
                           baseline_value=baseline_value, auction_value=auction_value)


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
        assert ov.market_value is None and ov.market_defaulted is True

    def test_non_ppr_row_without_adp_discloses_adp_fallback(self):
        # Value repriced (tier/points present) but per-format market ADP not populated.
        ov = overlay_for("p1", {"p1": _row(tier=4, pts=126.4, adp=None)}, "half_ppr")
        assert ov.tier == 4 and ov.projected_points == 126.4
        assert ov.adp_fantasypros is None and ov.adp_defaulted is True

    def test_non_ppr_row_carries_per_format_market_dollars(self):
        ov = overlay_for("p1", {"p1": _row(auction_value=33.0)}, "standard")
        assert ov.market_value == 33.0 and ov.market_defaulted is False

    def test_non_ppr_row_without_auction_value_discloses_market_fallback(self):
        ov = overlay_for("p1", {"p1": _row(tier=2, auction_value=None)}, "half_ppr")
        assert ov.market_value is None and ov.market_defaulted is True


class TestComputeFormatGap:
    def test_gap_is_ceiling_minus_market(self):
        assert compute_format_gap(40, 33) == (7.0, "market_undervalues")

    def test_negative_gap_reads_overvalued(self):
        assert compute_format_gap(20, 40) == (-20.0, "market_overvalues")

    def test_small_gap_is_aligned_in_both_directions(self):
        # The +/-5 band comes from engines.valuation, not a second copy of the numbers.
        assert compute_format_gap(40, 37)[1] == "aligned"
        assert compute_format_gap(37, 40)[1] == "aligned"

    def test_zero_gap_is_a_real_answer_not_missing_data(self):
        gap, signal = compute_format_gap(30, 30)
        assert gap == 0.0 and signal == "aligned"

    def test_missing_either_side_returns_no_gap(self):
        assert compute_format_gap(None, 30) == (None, None)
        assert compute_format_gap(30, None) == (None, None)


class TestResolveMarketAndGap:
    """The board must never print a gap that is not the difference of the two numbers
    beside it. PPR keeps the players-table trio; non-PPR recomputes."""

    def test_ppr_passes_the_players_table_trio_through_untouched(self):
        ov = overlay_for("p1", {}, "ppr")
        assert resolve_market_and_gap(ov, 61.0, 38.0, "market_undervalues", 33) == (
            61.0, 38.0, "market_undervalues",
        )

    def test_none_overlay_passes_through(self):
        assert resolve_market_and_gap(None, 61.0, 38.0, "aligned", 33) == (
            61.0, 38.0, "aligned",
        )

    def test_non_ppr_uses_format_market_and_recomputes_gap_from_shown_ceiling(self):
        ov = overlay_for("p1", {"p1": _row(ai_bid_ceiling=44, auction_value=30.0)}, "standard")
        market, gap, signal = resolve_market_and_gap(ov, 61.0, 38.0, "market_undervalues", 44)
        assert market == 30.0                    # the format's own $, not the PPR $61
        assert gap == 14.0                       # 44 - 30, not the stale PPR 38
        assert signal == "market_undervalues"

    def test_non_ppr_without_format_market_still_recomputes_against_shown_ppr_market(self):
        # Per-format DraftWizard $ not scraped yet: the market column falls back to PPR,
        # but the gap is computed against THAT number so the row stays self-consistent.
        ov = overlay_for("p1", {"p1": _row(ai_bid_ceiling=44, auction_value=None)}, "half_ppr")
        market, gap, signal = resolve_market_and_gap(ov, 61.0, 38.0, "market_undervalues", 44)
        assert market == 61.0
        assert gap == -17.0                      # 44 - 61
        assert signal == "market_overvalues"

    def test_non_ppr_with_no_market_at_all_yields_no_gap(self):
        ov = overlay_for("ghost", {}, "standard")
        assert resolve_market_and_gap(ov, None, 38.0, "market_undervalues", 44) == (
            None, None, None,
        )
