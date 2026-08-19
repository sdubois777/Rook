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
    repair_adp_within_position,
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
    # (2nd arg is now `tier`; past the window it's irrelevant.)
    assert classify_snake_flag(-359, 2, adp_rank=414) == "TARGET"
    # A big positive diff out past the window is also neutralized.
    assert classify_snake_flag(200, 1, adp_rank=500) == "TARGET"


def test_snake_flag_within_window_still_classifies():
    # Inside the window the normal thresholds apply. VALUE/SLEEPER is now driven
    # by the VORP tier (tier<=2 separator -> VALUE), not an absolute PPR bar.
    assert classify_snake_flag(-20, 2, adp_rank=30) == "REACH"
    assert classify_snake_flag(20, 2, adp_rank=30) == "VALUE"
    # Right at the boundary (180) is still draftable.
    assert classify_snake_flag(-20, 2, adp_rank=180) == "REACH"


def test_snake_flag_window_guard_optional():
    # adp_rank defaults to None -> no window guard (back-compat with old callers).
    assert classify_snake_flag(-20, 2) == "REACH"


# --- two-sided window: deep FantasyPros rank can't produce flag noise either ---

def test_snake_flag_neutralized_when_fp_rank_beyond_window():
    # The Singletary/Ford/Davis class: our adp_rank is inside the window but FP's
    # overall rank is undraftably deep (~400+), inflating the diff into a bogus
    # SLEEPER. The fp-side guard neutralizes it to TARGET (tier=5 here, near-repl).
    assert (
        classify_snake_flag(306, 5, adp_rank=99, fp_rank=405) == "TARGET"
    )
    # A positive diff with a real fp_rank but a near-replacement tier -> SLEEPER.
    assert (
        classify_snake_flag(20, 3, adp_rank=30, fp_rank=50) == "SLEEPER"
    )
    # Positive diff + separator tier -> VALUE.
    assert (
        classify_snake_flag(20, 2, adp_rank=30, fp_rank=50) == "VALUE"
    )


def test_snake_flag_fp_rank_at_boundary_still_classifies():
    # fp_rank exactly at the window (180) is still draftable -> normal thresholds.
    assert (
        classify_snake_flag(20, 2, adp_rank=30, fp_rank=180) == "VALUE"
    )


def test_snake_flag_fp_rank_guard_optional():
    # fp_rank defaults to None -> fp-side guard inert (back-compat).
    assert classify_snake_flag(20, 2, adp_rank=30) == "VALUE"


def test_snake_flag_value_separator_tier():
    # We rate them much earlier AND a genuine positional separator (tier 1-2)
    # -> VALUE.
    assert classify_snake_flag(20, 1) == "VALUE"
    assert classify_snake_flag(20, 2) == "VALUE"


def test_snake_flag_sleeper_near_replacement_tier():
    # We rate them much earlier BUT near-replacement (tier 3+) -> SLEEPER.
    assert classify_snake_flag(20, 3) == "SLEEPER"
    assert classify_snake_flag(20, 5) == "SLEEPER"


def test_snake_flag_sleeper_when_tier_missing():
    # No tier (player not valued / no projection) can't be confirmed a separator
    # -> SLEEPER on a positive diff, never VALUE.
    assert classify_snake_flag(20, None, adp_rank=30) == "SLEEPER"


def test_snake_flag_target_consensus():
    assert classify_snake_flag(5, 2) == "TARGET"
    assert classify_snake_flag(-10, 2) == "TARGET"


def test_snake_flag_reach_negative_diff():
    assert classify_snake_flag(-20, 2) == "REACH"


def test_snake_flag_null_adp_defaults_target():
    assert classify_snake_flag(None, 2) == "TARGET"


def test_snake_flag_value_is_position_agnostic_via_tier():
    # The classifier no longer uses an absolute per-position PPR bar; position-
    # relativity lives in `tier` (PAR ratio) upstream. So a tier-2 separator is
    # VALUE and a tier-3 near-replacement player is SLEEPER regardless of position
    # — fixing the old asymmetry where a ~200-PPR TE was VALUE but an equally
    # strong ~200-PPR WR was SLEEPER.
    assert classify_snake_flag(20, 2, adp_rank=30) == "VALUE"
    assert classify_snake_flag(20, 3, adp_rank=30) == "SLEEPER"


def test_snake_flag_not_in_model_prompt():
    # snake_flag is computed deterministically (it depends on adp_diff, which the
    # model can't know at inference) — it must NOT be in the output schema.
    assert '"snake_flag"' not in SYSTEM_PROMPT
    assert "SNAKE DRAFT FLAGS" not in SYSTEM_PROMPT


def test_classify_snake_flag_null_adp():
    assert classify_snake_flag(None, 1) == "TARGET"


def test_classify_snake_flag_high_diff_separator_tier():
    # diff +25, tier 1 (clear separator) -> VALUE
    assert classify_snake_flag(25, 1) == "VALUE"


def test_classify_snake_flag_high_diff_near_replacement_tier():
    # diff +25, tier 4 (near replacement) -> SLEEPER
    assert classify_snake_flag(25, 4) == "SLEEPER"


def test_classify_snake_flag_reach():
    # diff -20 -> REACH regardless of tier
    assert classify_snake_flag(-20, 1) == "REACH"
    assert classify_snake_flag(-20, 5) == "REACH"


def test_prompt_auction_note_no_dollar_instruction():
    # auction_note must be told to avoid dollar amounts (shared with snake).
    assert "NO dollar amounts" in SYSTEM_PROMPT


def test_valuation_agent_version_defined():
    # The version invalidates the (players + version) cache on a prompt/context change.
    # It is bumped whenever the agent's inputs change (v7: trigger_condition + reasoning;
    # v8: re-reason after the roster_changes displaced-direction fix). Assert the SHAPE
    # ("v<N>") rather than a fixed value so a legitimate bump doesn't fail this test.
    import re
    assert re.fullmatch(r"v\d+", VALUATION_AGENT_VERSION), VALUATION_AGENT_VERSION


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


# ---------------------------------------------------------------------------
# repair_adp_within_position — the snake pick must not contradict the dollars
# ---------------------------------------------------------------------------
def _pl(pid, pos, adp, pts):
    return SimpleNamespace(id=pid, position=pos, adp_ai=adp, adjusted_points=pts)


def test_repair_gives_the_better_projection_the_earlier_pick():
    # The model priced these correctly and picked them backwards.
    a = _pl("a", "TE", 22.0, 160.2)   # worse projection, earlier pick
    b = _pl("b", "TE", 38.0, 198.5)   # better projection, later pick
    changed = repair_adp_within_position([a, b])
    assert changed == 2
    assert b.adp_ai == 22.0
    assert a.adp_ai == 38.0


def test_repair_preserves_the_positions_own_picks():
    # Each position keeps the exact set of picks it started with — the repair
    # reassigns, it never invents or moves a pick between positions.
    tes = [_pl("t1", "TE", 18.0, 160.0), _pl("t2", "TE", 22.0, 220.0),
           _pl("t3", "TE", 52.0, 198.0)]
    rbs = [_pl("r1", "RB", 4.0, 261.0), _pl("r2", "RB", 8.0, 385.0)]
    repair_adp_within_position(tes + rbs)
    assert sorted(p.adp_ai for p in tes) == [18.0, 22.0, 52.0]
    assert sorted(p.adp_ai for p in rbs) == [4.0, 8.0]


def test_repair_does_not_move_picks_across_positions():
    # A TE must never be handed a RB's pick, however the projections compare.
    te = _pl("t", "TE", 60.0, 400.0)   # best projection on the board
    rb = _pl("r", "RB", 3.0, 100.0)    # worst projection, earliest pick
    repair_adp_within_position([te, rb])
    assert te.adp_ai == 60.0
    assert rb.adp_ai == 3.0


def test_repair_leaves_players_without_a_projection_alone():
    # No adjusted_points means no way to order him — the model's pick stands.
    known = _pl("k", "TE", 40.0, 150.0)
    unknown = _pl("u", "TE", 20.0, None)
    changed = repair_adp_within_position([known, unknown])
    assert changed == 0
    assert unknown.adp_ai == 20.0
    assert known.adp_ai == 40.0


def test_repair_leaves_players_without_a_position_alone():
    orphan = _pl("o", None, 30.0, 200.0)
    changed = repair_adp_within_position([orphan])
    assert changed == 0
    assert orphan.adp_ai == 30.0


def test_repair_is_a_no_op_when_the_order_is_already_right():
    good = [_pl("a", "WR", 5.0, 300.0), _pl("b", "WR", 9.0, 250.0),
            _pl("c", "WR", 14.0, 200.0)]
    assert repair_adp_within_position(good) == 0
    assert [p.adp_ai for p in good] == [5.0, 9.0, 14.0]


def test_repair_is_stable_across_reruns_when_projections_tie():
    # Equal projections must not reshuffle on a second pipeline run.
    first = [_pl("a", "RB", 12.0, 200.0), _pl("b", "RB", 30.0, 200.0)]
    repair_adp_within_position(first)
    snapshot = [(p.id, p.adp_ai) for p in first]
    repair_adp_within_position(first)
    assert [(p.id, p.adp_ai) for p in first] == snapshot


def test_repair_fixes_the_reported_tight_end_board():
    # Regression for the board a user reported: Bowers came out TE2 by pick and
    # TE5 by price off the same projection. Numbers are the measured dev board.
    board = [
        _pl("mcbride",  "TE", 18.0, 220.0),
        _pl("bowers",   "TE", 22.0, 160.2),
        _pl("warren",   "TE", 28.0, 205.0),
        _pl("loveland", "TE", 38.0, 198.5),
        _pl("pitts",    "TE", 52.0, 198.0),
    ]
    repair_adp_within_position(board)
    by_pick = sorted(board, key=lambda p: p.adp_ai)
    assert [p.id for p in by_pick] == [
        "mcbride", "warren", "loveland", "pitts", "bowers",
    ]
    # Bowers now sits last among these five by pick, exactly as he does by price.
    assert next(p for p in board if p.id == "bowers").adp_ai == 52.0
