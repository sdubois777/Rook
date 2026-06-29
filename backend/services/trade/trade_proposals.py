"""
Trade proposals — the system finds trades. A proposal is just an analyze the
SYSTEM constructed instead of the user, so this REUSES slice-3's deterministic
verdict (``analyze_trade``) — it does NOT reimplement evaluation.

Flow: candidates (LLM-generated, with a deterministic enumerator fallback) →
each run through ``analyze_trade`` → keep only those whose verdict clears the v1
benefit bar → rank by user value gain → cap at 5. The never-pad guarantee lives
HERE, in deterministic Python: regardless of how many candidates come in, only
bar-clearing ones surface, and an empty result is a first-class outcome.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.services.trade.league_state import LeagueState
from backend.services.trade.trade_analysis import (
    TradeAnalysis,
    analyze_trade,
    validate_trade,
)
from backend.services.trade.value_engine import InSeasonValue

MAX_PROPOSALS = 5


@dataclass(frozen=True)
class Candidate:
    give_ids: tuple[str, ...]
    get_ids: tuple[str, ...]
    counterparty_team_id: str


def enumerate_candidates(state: LeagueState, my_team_id: str) -> list[Candidate]:
    """Deterministic fallback search: every 1-for-1 swap between my roster and
    each opponent's roster. Plausibility (need/surplus targeting) is the LLM's
    job; the deterministic verdict + bar is what actually decides what surfaces,
    so an exhaustive 1-for-1 enumeration is a safe, non-random fallback."""
    my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
    if my_team is None:
        return []
    out: list[Candidate] = []
    for opp in state.teams:
        if opp.team_id == my_team_id:
            continue
        for mp in my_team.roster:
            for op in opp.roster:
                out.append(Candidate(
                    (mp.canonical_player_id,), (op.canonical_player_id,), opp.team_id,
                ))
    return out


def _clears_bar(analysis: TradeAnalysis) -> bool:
    """v1 'good enough to surface' threshold: the deterministic verdict must name
    YOU the winner — i.e. the user nets value beyond the fairness epsilon
    (value_delta > the analyzer's fair band). A merely-'even' trade does NOT
    surface. Hedged trades still surface (winner stays 'you') carrying their
    hedge — the engine already softened them; we don't re-manufacture a blowout."""
    return analysis.winner == "you"


def evaluate_candidates(
    state: LeagueState,
    values: dict[str, InSeasonValue],
    my_team_id: str,
    candidates: list[Candidate],
    *,
    roster_limit: int,
    max_results: int = MAX_PROPOSALS,
) -> list[tuple[Candidate, TradeAnalysis]]:
    """Run each candidate through slice-3's verdict, keep bar-clearing ones,
    rank by user value gain, cap. Pure + deterministic — the never-pad + cap
    guarantees are proven here, independent of the LLM."""
    scored: list[tuple[Candidate, TradeAnalysis]] = []
    seen: set[tuple] = set()
    for cand in candidates:
        key = (cand.give_ids, cand.get_ids)
        if key in seen:
            continue
        seen.add(key)
        try:
            validate_trade(state, values, my_team_id, list(cand.give_ids), list(cand.get_ids))
            analysis = analyze_trade(
                state, values, my_team_id,
                list(cand.give_ids), list(cand.get_ids), roster_limit=roster_limit,
            )
        except Exception:
            continue  # unresolvable candidate — skip, never surface
        if _clears_bar(analysis):
            scored.append((cand, analysis))
    scored.sort(key=lambda ca: ca[1].value_delta, reverse=True)
    return scored[:max_results]
