"""Unit tests for the snake-ADP additions to valuation_agent.

Covers the deterministic pieces: the position clamp and the prompt wiring. The
LLM-generated adp_ai itself isn't unit-testable, but the clamp guarantees a QB
the model over-ranks at pick 5 gets pushed to the late QB floor.
"""
from __future__ import annotations

from types import SimpleNamespace

from backend.agents.valuation_agent import (
    ADP_POSITION_RANGES,
    DRAFTABLE_WINDOW,
    SYSTEM_PROMPT,
    VALUATION_AGENT_VERSION,
    VALUATION_SCORING,
    assign_adp_ranks,
    classify_snake_flag,
    clamp_adp,
    compute_adp_diff,
)


def test_qb_floor_at_least_40():
    # QBs go late in snake — pick 5 is clamped up to the QB floor (40). The floor
    # was raised 25->40 because QBs ranked 15-20+ picks ahead of FP consensus.
    assert clamp_adp(5, "QB") == 40
    assert ADP_POSITION_RANGES["QB"][0] >= 40


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
    # QB floor 40 (raised from 25) keeps QBs from being drafted too early; K/DEF
    # stay last.
    assert ADP_POSITION_RANGES["QB"][0] == 40
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


# --- snake polish: adp_diff, adp_rank, snake_flag ---

def test_adp_diff_computed_correctly():
    # consensus 18 - us 3 = +15 (we rate them 15 picks earlier than FP)
    assert compute_adp_diff(18, 3) == 15.0
    assert compute_adp_diff(3, 18) == -15.0
    assert compute_adp_diff(None, 3) is None
    assert compute_adp_diff(18, None) is None


def test_adp_rank_sequential_1_to_n():
    players = [SimpleNamespace(adp_rank=None) for _ in range(5)]
    n = assign_adp_ranks(players)
    assert n == 5
    assert [p.adp_rank for p in players] == [1, 2, 3, 4, 5]


# --- adp_diff computed from adp_rank, not adp_ai (the displayed-column fix) ---

def _top_tied_players():
    # Real prod shape: three players TIED on adp_ai=4.0 but distinct fp ranks.
    # After assign_adp_ranks they get clean ranks 1, 2, 3 (the "AI ADP" shown).
    return [
        SimpleNamespace(name="Bijan", adp_ai=4.0, adp_rank=None, adp_fantasypros=2.0),
        SimpleNamespace(name="Gibbs", adp_ai=4.0, adp_rank=None, adp_fantasypros=1.0),
        SimpleNamespace(name="Chase", adp_ai=4.0, adp_rank=None, adp_fantasypros=3.0),
    ]


def test_adp_diff_computed_from_adp_rank_not_adp_ai():
    players = _top_tied_players()
    assign_adp_ranks(players)  # ranks 1, 2, 3
    diffs = {p.name: compute_adp_diff(p.adp_fantasypros, p.adp_rank) for p in players}
    assert diffs == {"Bijan": 1.0, "Gibbs": -1.0, "Chase": 0.0}


def test_adp_diff_positive_when_fp_ranks_later():
    # Amon-Ra: fp_rank 7, our rank 4 -> FP ranks him LATER -> +3 (we like him more)
    assert compute_adp_diff(7, 4) == 3.0


def test_adp_diff_negative_when_fp_ranks_earlier():
    # CMC: fp_rank 6, our rank 7 -> FP ranks him EARLIER -> -1 (market likes him more)
    assert compute_adp_diff(6, 7) == -1.0


def test_bijan_adp_diff_is_plus_one_not_minus_two():
    # The canonical regression: Bijan shows AI ADP=1 (adp_rank), FP ADP=2.
    # Diff against adp_rank(1) = +1 (correct, matches the board).
    assert compute_adp_diff(2, 1) == 1.0
    # Diff against adp_ai(4) = -2 (the OLD bug — must NOT be what we compute).
    assert compute_adp_diff(2, 4) == -2.0


# --- draftable-window guard: deep players can't produce flag noise ---

def test_draftable_window_is_180():
    assert DRAFTABLE_WINDOW == 180


def test_snake_flag_neutralized_beyond_draftable_window():
    # Mike Evans artifact: huge negative diff but adp_rank 414 (round ~35).
    # Past the window the diff is rank-scale noise -> TARGET, not REACH.
    assert classify_snake_flag(-359, 240, "WR", adp_rank=414) == "TARGET"
    # A big positive diff out past the window is also neutralized.
    assert classify_snake_flag(200, 300, "WR", adp_rank=500) == "TARGET"


def test_snake_flag_within_window_still_classifies():
    # Inside the window the normal thresholds apply.
    assert classify_snake_flag(-20, 240, "WR", adp_rank=30) == "REACH"
    assert classify_snake_flag(20, 280, "WR", adp_rank=30) == "VALUE"
    # Right at the boundary (180) is still draftable.
    assert classify_snake_flag(-20, 240, "WR", adp_rank=180) == "REACH"


def test_snake_flag_window_guard_optional():
    # adp_rank defaults to None -> no window guard (back-compat with old callers).
    assert classify_snake_flag(-20, 240, "WR") == "REACH"


# --- two-sided window: deep FantasyPros rank can't produce flag noise either ---

def test_snake_flag_neutralized_when_fp_rank_beyond_window():
    # The Singletary/Ford/Davis class: our adp_rank is inside the window but FP's
    # overall rank is undraftably deep (~400+), inflating the diff into a bogus
    # SLEEPER. The fp-side guard neutralizes it to TARGET.
    assert (
        classify_snake_flag(306, 120, "RB", adp_rank=99, fp_rank=405) == "TARGET"
    )
    # A big positive diff with a real, draftable fp_rank still classifies normally.
    assert (
        classify_snake_flag(20, 120, "RB", adp_rank=30, fp_rank=50) == "SLEEPER"
    )
    assert (
        classify_snake_flag(20, 280, "WR", adp_rank=30, fp_rank=50) == "VALUE"
    )


def test_snake_flag_fp_rank_at_boundary_still_classifies():
    # fp_rank exactly at the window (180) is still draftable -> normal thresholds.
    assert (
        classify_snake_flag(20, 280, "WR", adp_rank=30, fp_rank=180) == "VALUE"
    )


def test_snake_flag_fp_rank_guard_optional():
    # fp_rank defaults to None -> fp-side guard inert (back-compat; this is the
    # path the no-fp_rank unit calls above exercise).
    assert classify_snake_flag(20, 280, "WR", adp_rank=30) == "VALUE"


def test_snake_flag_value_high_production():
    # We rate them much earlier AND strong WR production -> VALUE
    assert classify_snake_flag(20, 280, "WR") == "VALUE"


def test_snake_flag_sleeper_low_production():
    # We rate them much earlier BUT modest production -> SLEEPER
    assert classify_snake_flag(20, 120, "WR") == "SLEEPER"


def test_snake_flag_target_consensus():
    assert classify_snake_flag(5, 280, "WR") == "TARGET"
    assert classify_snake_flag(-10, 280, "WR") == "TARGET"


def test_snake_flag_reach_negative_diff():
    assert classify_snake_flag(-20, 280, "WR") == "REACH"


def test_snake_flag_null_adp_defaults_target():
    assert classify_snake_flag(None, 280, "WR") == "TARGET"


def test_snake_flag_position_relative_production():
    # A QB at 280 PPR is below-average -> SLEEPER even with a big diff;
    # a TE at 180 clears the lower TE bar -> VALUE.
    assert classify_snake_flag(20, 280, "QB") == "SLEEPER"
    assert classify_snake_flag(20, 180, "TE") == "VALUE"


def test_snake_flag_not_in_model_prompt():
    # snake_flag is computed deterministically (it depends on adp_diff, which the
    # model can't know at inference) — it must NOT be in the output schema.
    assert '"snake_flag"' not in SYSTEM_PROMPT
    assert "SNAKE DRAFT FLAGS" not in SYSTEM_PROMPT


def test_classify_snake_flag_null_adp():
    assert classify_snake_flag(None, 380, "QB") == "TARGET"


def test_classify_snake_flag_high_diff_high_ppr():
    # diff +25, QB projecting 380 (strong) -> VALUE
    assert classify_snake_flag(25, 380, "QB") == "VALUE"


def test_classify_snake_flag_high_diff_low_ppr():
    # diff +25, TE projecting 90 (modest) -> SLEEPER
    assert classify_snake_flag(25, 90, "TE") == "SLEEPER"


def test_classify_snake_flag_reach():
    # diff -20 -> REACH regardless of production
    assert classify_snake_flag(-20, 400, "QB") == "REACH"
    assert classify_snake_flag(-20, 50, "TE") == "REACH"


def test_prompt_auction_note_no_dollar_instruction():
    # auction_note must be told to avoid dollar amounts (shared with snake).
    assert "NO dollar amounts" in SYSTEM_PROMPT


def test_valuation_agent_version_defined():
    # Bumped to v4 with the QB 5-tier framework to invalidate the v3 cache.
    assert VALUATION_AGENT_VERSION == "v4"


def test_prompt_qb_floor_is_pick_40():
    # The prompt must forbid any QB before pick 40 (raised from 25).
    assert "QB ADP" in SYSTEM_PROMPT
    assert "NEVER before pick 40" in SYSTEM_PROMPT
    assert "DEEPEST position" in SYSTEM_PROMPT


def test_prompt_has_qb_five_tier_framework():
    # Five tiers including the "startable streamer" band that v3 lacked.
    assert "5-TIER FRAMEWORK" in SYSTEM_PROMPT
    assert "Lamar Jackson ONLY" in SYSTEM_PROMPT
    assert "picks 40-50" in SYSTEM_PROMPT    # Lamar
    assert "picks 55-70" in SYSTEM_PROMPT    # elite passers
    assert "picks 80-110" in SYSTEM_PROMPT   # strong starters
    assert "Startable streamers" in SYSTEM_PROMPT
    assert "picks 110-140" in SYSTEM_PROMPT  # startable streamers
    assert "picks 145-170" in SYSTEM_PROMPT  # backups


def test_prompt_qb_anti_cluster_rule():
    assert "ANTI-CLUSTER RULE" in SYSTEM_PROMPT
    assert "Minimum 8-pick gap" in SYSTEM_PROMPT
    assert "Maximum 6 QBs in any 30-pick window" in SYSTEM_PROMPT
    assert "Do NOT stack QBs at the cap" in SYSTEM_PROMPT
