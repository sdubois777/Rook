"""Unit tests for the snake-ADP additions to valuation_agent.

Covers the deterministic pieces: the position clamp and the prompt wiring. The
LLM-generated adp_ai itself isn't unit-testable, but the clamp guarantees a QB
the model over-ranks at pick 5 gets pushed to the late QB floor.
"""
from __future__ import annotations

from backend.agents.valuation_agent import (
    ADP_POSITION_RANGES,
    SYSTEM_PROMPT,
    VALUATION_SCORING,
    clamp_adp,
)


def test_clamp_adp_qb_floored_late():
    # QBs go late in snake — pick 5 is clamped up to the QB floor (25). The
    # floor is 25 (not 50) so elite QBs like Allen can land ~rd 3 where they go.
    assert clamp_adp(5, "QB") == 25


def test_clamp_adp_within_range_unchanged():
    assert clamp_adp(24.0, "WR") == 24.0


def test_clamp_adp_kicker_and_def_floored_late():
    assert clamp_adp(10, "K") == 140
    assert clamp_adp(20, "DEF") == 130


def test_clamp_adp_caps_at_position_high():
    assert clamp_adp(250, "RB") == 100  # RB high bound


def test_clamp_adp_none_passthrough():
    assert clamp_adp(None, "RB") is None


def test_clamp_adp_unknown_position_full_range():
    assert clamp_adp(150, "P") == 150  # falls back to (1, 200)


def test_adp_position_ranges_qb_def_k_late():
    # QB floor 25 keeps elite QBs from being clamped too late; K/DEF stay last.
    assert ADP_POSITION_RANGES["QB"][0] == 25
    assert ADP_POSITION_RANGES["K"][0] >= 140
    assert ADP_POSITION_RANGES["DEF"][0] >= 130


def test_prompt_has_snake_adp_section():
    # Lock the inversion guidance into the prompt so it can't silently regress.
    assert "SNAKE DRAFT ADP" in SYSTEM_PROMPT
    assert "adp_ai" in SYSTEM_PROMPT
    assert "OPPOSITE of bid ceiling" in SYSTEM_PROMPT
    assert "LOWER numbers = earlier picks" in SYSTEM_PROMPT


def test_valuation_scoring_default_ppr():
    assert VALUATION_SCORING == "ppr"


def test_prompt_marks_adp_ai_mandatory():
    # The Sonnet path was silently omitting adp_ai for top tiers; the prompt must
    # now demand it explicitly.
    assert "MANDATORY" in SYSTEM_PROMPT
    assert "REQUIRED, never null" in SYSTEM_PROMPT
    # Tier-midpoint fallback so the model always has a value to emit.
    assert "tier midpoint" in SYSTEM_PROMPT
    assert "Tier 1 → 6" in SYSTEM_PROMPT


def test_prompt_lists_adp_ai_before_bid_ceiling():
    # adp_ai must come early in the JSON schema so a truncated response still
    # includes it (it was last before, and Sonnet dropped it).
    assert SYSTEM_PROMPT.index('"adp_ai"') < SYSTEM_PROMPT.index('"ai_bid_ceiling"')


def test_clamp_adp_qb_caps_at_170():
    # Streaming QBs cap at 170 so they still get drafted, not skipped.
    assert clamp_adp(250, "QB") == 170


def test_prompt_has_qb_tier_differentiation():
    # The model was clustering all QBs at ~38; the prompt must spread them by
    # tier and tell it to wait on QB.
    assert "QB ADP guidance" in SYSTEM_PROMPT
    assert "picks 25-40" in SYSTEM_PROMPT  # elite
    assert "picks 45-80" in SYSTEM_PROMPT  # strong
    assert "picks 85-130" in SYSTEM_PROMPT  # standard starter
    assert "Wait on QB" in SYSTEM_PROMPT
    assert "NEVER cluster" in SYSTEM_PROMPT
