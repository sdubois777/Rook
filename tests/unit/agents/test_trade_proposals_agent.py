"""
TradeProposalsAgent.generate_candidates — hot-path wiring (enumerator UNION).

The recon bug: the slice-6 targeted enumerator ran ONLY as a fallback when the LLM
returned empty, so on every successful hot path it was dead code. The fix runs BOTH
generators and unions (deduped) their candidates, so the deterministic surplus-for-
need / multi-player trades reach the edge-band gate on every call. These tests stub
the LLM (call_once) so they're CI-safe — no network — and assert: enumerator
candidates are now evaluated, union+dedup, LLM-empty / LLM-fails graceful
degradation, never-pad, and the combined pool stays bounded.
"""
from __future__ import annotations

import json
from math import comb
from unittest.mock import AsyncMock

from backend.agents.trade_proposals import TradeProposalsAgent
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_proposals import (
    Candidate,
    _candidate_key,
    evaluate_candidates,
)
from backend.services.trade.value_engine import Confidence, InSeasonValue, ValueTrend


def _iv(pid, pos, fv):
    return InSeasonValue(
        canonical_player_id=pid, name=f"P-{pid}", position=pos, forward_value=fv,
        value_trend=ValueTrend.STABLE, buy_low=False, sell_high=False, why="",
        games_played=10, usage_recent=0.5, usage_prior=0.5, usage_delta=0.0,
        recency_ppg=fv, expected_ppg=fv, opportunity_gap=0.0, sustainable=True,
        forward_ppg=fv, schedule_modifier=0.0, prior_projection=None,
        prior_weight=0.0, name_bias_guard_applied=False, confidence=Confidence.FULL,
        confidence_reason="",
    )


def _league(my_spec, opp_spec):
    me = TeamState("me", "Me", True,
                   tuple(RosterPlayer(p, f"P-{p}", pos) for p, pos, _ in my_spec))
    opp = TeamState("opp", "Opp", False,
                    tuple(RosterPlayer(p, f"P-{p}", pos) for p, pos, _ in opp_spec))
    state = LeagueState(2025, 14, (me, opp))
    values = {p: _iv(p, pos, fv) for p, pos, fv in (*my_spec, *opp_spec)}
    return state, values


def _agent_with_llm(raw):
    """A real agent with call_once stubbed (raw str returned, or an exception
    raised if `raw` is an Exception) — no network."""
    agent = TradeProposalsAgent()
    agent.call_once = AsyncMock(
        side_effect=raw if isinstance(raw, Exception) else None,
        return_value=None if isinstance(raw, Exception) else raw,
    )
    return agent


# Gronk-shaped: strong everywhere but a badly WEAK QB (need), with RB depth to spend;
# the opponent is RB-thin and holds a SURPLUS QB that's a big upgrade for me. The
# broadened enumerator pays for that QB out of my RB depth — give(rb2) -> get(surplusQB),
# a value-fair swap (RB2 20 ~= surplus QB 20) that clears the LINEUP gate (my QB jump
# dwarfs the RB2 downgrade, and the RB-thin opponent maintains by adding a real RB).
GRONK = [("weakQB", "QB", 5), ("rb1", "RB", 22), ("rb2", "RB", 20), ("surplusRB", "RB", 14),
         ("wr1", "WR", 18), ("wr2", "WR", 17), ("wr3", "WR", 16), ("wr4", "WR", 16),
         ("te", "TE", 14), ("te2", "TE", 12)]
OPP = [("oQB1", "QB", 24), ("surplusQB", "QB", 20), ("orb1", "RB", 8), ("orb2", "RB", 7),
       ("owr1", "WR", 20), ("owr2", "WR", 18), ("owr3", "WR", 16), ("owr4", "WR", 15),
       ("ote", "TE", 14)]

_GOOD = Candidate(("rb2",), ("surplusQB",), "opp")   # what the enumerator builds


# ---------------------------------------------------------------------------
# HEADLINE — enumerator candidates are now evaluated, and a known-good one clears
# ---------------------------------------------------------------------------
async def test_enumerator_candidates_are_evaluated_and_surface_on_hot_path():
    state, values = _league(GRONK, OPP)
    # LLM proposes only a different (junk) trade — it would have been the ONLY
    # thing evaluated under the old fallback logic.
    llm_raw = json.dumps([{"give": ["te2"], "get": ["ote"], "team_id": "opp"}])
    agent = _agent_with_llm(llm_raw)

    combined = await agent.generate_candidates(state, "me", values)
    assert _GOOD in combined                                   # enumerator's trade IS in the pool
    assert Candidate(("te2",), ("ote",), "opp") in combined    # LLM's trade too (union)

    surfaced = evaluate_candidates(state, values, "me", combined, roster_limit=16)
    shapes = {(c.give_ids, c.get_ids) for c, _, _ in surfaced}
    assert (("rb2",), ("surplusQB",)) in shapes                # the dead trade now clears + surfaces


# ---------------------------------------------------------------------------
# UNION + DEDUP — a trade from BOTH generators appears once
# ---------------------------------------------------------------------------
async def test_trade_from_both_generators_is_deduped_to_one():
    state, values = _league(GRONK, OPP)
    # LLM proposes the SAME trade the enumerator builds.
    llm_raw = json.dumps([{"give": ["rb2"], "get": ["surplusQB"], "team_id": "opp"}])
    agent = _agent_with_llm(llm_raw)

    combined = await agent.generate_candidates(state, "me", values)
    occurrences = sum(1 for c in combined if _candidate_key(c) == _candidate_key(_GOOD))
    assert occurrences == 1                                    # counted once, not twice


# ---------------------------------------------------------------------------
# GRACEFUL DEGRADATION — LLM empty / LLM raises still evaluates the enumerator
# ---------------------------------------------------------------------------
async def test_llm_empty_still_evaluates_enumerator():
    state, values = _league(GRONK, OPP)
    agent = _agent_with_llm("[]")                              # LLM returns nothing
    combined = await agent.generate_candidates(state, "me", values)
    assert _GOOD in combined                                   # old fallback behavior preserved


async def test_llm_failure_still_runs_enumerator_without_crashing():
    state, values = _league(GRONK, OPP)
    agent = _agent_with_llm(RuntimeError("model down"))        # LLM raises
    combined = await agent.generate_candidates(state, "me", values)
    assert _GOOD in combined                                   # enumerator stands alone, no crash


# ---------------------------------------------------------------------------
# NEVER-PAD — a bigger pool is more CHANCES, not more surface
# ---------------------------------------------------------------------------
async def test_never_pads_even_with_both_generators_contributing():
    # Strictly-dominant me (every player starts → no surplus → enumerator empty);
    # the LLM proposes a valid but bad trade (give a stud for a scrub). It IS in
    # the pool and gets evaluated, but nothing clears → empty, not padded.
    dom_me = [("q", "QB", 25), ("r1", "RB", 24), ("r2", "RB", 22), ("w1", "WR", 20),
              ("w2", "WR", 18), ("w3", "WR", 16), ("t", "TE", 14), ("br", "RB", 13)]
    weak_them = [("oq", "QB", 10), ("or1", "RB", 9), ("or2", "RB", 7), ("ow1", "WR", 8),
                 ("ow2", "WR", 6), ("ow3", "WR", 5), ("ot", "TE", 4)]
    state, values = _league(dom_me, weak_them)
    llm_raw = json.dumps([{"give": ["r1"], "get": ["ow1"], "team_id": "opp"}])
    agent = _agent_with_llm(llm_raw)

    combined = await agent.generate_candidates(state, "me", values)
    assert Candidate(("r1",), ("ow1",), "opp") in combined     # LLM trade evaluated...
    surfaced = evaluate_candidates(state, values, "me", combined, roster_limit=16)
    assert surfaced == []                                      # ...but nothing clears → empty


# ---------------------------------------------------------------------------
# EFFICIENCY — the union stays bounded, orders of magnitude below brute force
# ---------------------------------------------------------------------------
async def test_combined_pool_stays_bounded():
    me_strong = [("qm", "QB", 22), ("rm1", "RB", 24), ("rm2", "RB", 22), ("rm3", "RB", 20),
                 ("rm4", "RB", 15), ("rm5", "RB", 13), ("wm1", "WR", 16), ("wm2", "WR", 14),
                 ("tm", "TE", 15)]
    them = [("qt", "QB", 19), ("rt1", "RB", 9), ("btr", "RB", 7), ("wt1", "WR", 20),
            ("wt2", "WR", 18), ("wt3", "WR", 16), ("wt4", "WR", 14), ("wt5", "WR", 13),
            ("wt6", "WR", 12), ("tt", "TE", 14)]
    state, values = _league(me_strong, them)
    # LLM adds a few candidates on top of the enumerator's.
    llm_raw = json.dumps([
        {"give": ["rm4"], "get": ["wt1"], "team_id": "opp"},
        {"give": ["rm5"], "get": ["wt2"], "team_id": "opp"},
    ])
    agent = _agent_with_llm(llm_raw)
    combined = await agent.generate_candidates(state, "me", values)

    brute = sum(comb(9, k) for k in (1, 2, 3)) * sum(comb(10, k) for k in (1, 2, 3))
    assert len(combined) <= 50                 # bounded
    assert len(combined) * 100 < brute         # orders of magnitude below brute force
