"""
Trade Analyzer agent (Sonnet) — writes the human-readable rationale for a trade
verdict that has ALREADY been decided deterministically from engine value
(backend/services/trade/trade_analysis.py).

Architecture: a single ``call_once`` (Sonnet — multi-step causal reasoning per
the model rule), NOT an iterative tool loop. The agent explains/judges; it does
NOT recompute value and MUST NOT override the winner or re-introduce the
reputation-based reasoning the engine stripped out. If the call fails it falls
back to a templated rationale so the route still returns a verdict.
"""
from __future__ import annotations

import logging

from backend.agents.base_agent import SONNET, BaseAgent
from backend.services.trade.trade_analysis import TradeAnalysis

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a fantasy-football trade analyst. A deterministic value engine has
ALREADY decided this trade's verdict from in-season usage data. Your job is to
EXPLAIN that verdict in 2-3 sentences — not to change it.

Hard rules:
- Do NOT override the winner/fairness; they are fixed inputs.
- Ground every claim in the provided usage signals (snap %, target share trend,
  forward value, buy/sell flags). NEVER argue from a player's name, draft
  pedigree, or reputation.
- If the verdict is hedged (a player has limited/insufficient confidence or a
  team change in the window), say so plainly and keep the tone tentative — do
  not assert a crisp winner off thin or cross-team data.
- If a roster-drop warning is present, mention it.
Return only the explanation text."""


def _fmt_player(p) -> str:
    flags = []
    if p.buy_low:
        flags.append("buy-low")
    if p.sell_high:
        flags.append("sell-high")
    flag = f" [{', '.join(flags)}]" if flags else ""
    return (
        f"  - {p.name} ({p.position}): forward_value={p.forward_value}, "
        f"trend={p.value_trend}, confidence={p.confidence}{flag}. why: {p.why}"
    )


def format_user_prompt(a: TradeAnalysis) -> str:
    lines = [
        f"Verdict (FIXED): winner={a.winner}, fairness={a.fairness}, "
        f"value_delta={a.value_delta} (you get {a.get_value} vs give {a.give_value}).",
        f"Confidence floor: {a.confidence}. Hedged: {a.hedged}."
        + (f" Hedge reason: {a.hedge_reason}." if a.hedged else ""),
        "You GIVE:",
        *[_fmt_player(p) for p in a.give],
        "You GET:",
        *[_fmt_player(p) for p in a.get],
    ]
    if a.roster_guard.triggered:
        lines.append(f"Roster warning: {a.roster_guard.message}")
    lines.append("Explain this verdict in 2-3 sentences, grounded only in the usage signals above.")
    return "\n".join(lines)


def fallback_rationale(a: TradeAnalysis) -> str:
    base = (
        f"{a.fairness.capitalize()} — you net {a.value_delta:+} forward-value "
        f"({a.get_value} for {a.give_value}), based on in-season usage."
    )
    if a.hedged:
        base += f" Treat with caution: {a.hedge_reason}."
    if a.roster_guard.triggered:
        base += f" {a.roster_guard.message}"
    return base


class TradeAnalyzerAgent(BaseAgent):
    AGENT_NAME = "trade_analyzer"
    AGENT_MODEL = SONNET
    AGENT_MAX_TOKENS = 500

    async def explain_trade(self, analysis: TradeAnalysis) -> str:
        """Generate the grounded rationale (Sonnet); fall back to a template on
        any failure so the route always returns a verdict."""
        user = format_user_prompt(analysis)
        input_data = {
            "winner": analysis.winner, "fairness": analysis.fairness,
            "value_delta": analysis.value_delta, "hedged": analysis.hedged,
            "give": [g.canonical_player_id for g in analysis.give],
            "get": [g.canonical_player_id for g in analysis.get],
        }
        try:
            raw = await self.call_once(
                system=_SYSTEM_PROMPT, user=user, input_data=input_data,
                entity_id=analysis.my_team_id,
            )
        except Exception as exc:  # network/model failure → still return a verdict
            logger.warning("trade rationale generation failed: %s", exc)
            return fallback_rationale(analysis)
        text = (raw or "").strip()
        return text or fallback_rationale(analysis)
