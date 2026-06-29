"""
Trade Proposals agent (Sonnet) — generates CANDIDATE trades between the user's
roster and the league's other rosters. The LLM's only jobs are candidate
generation (target real needs/surplus, not random pairings) and — via the
analyzer agent — the one-line "why". It does NOT decide which candidates
surface: that is slice-3's deterministic verdict + the benefit bar
(backend/services/trade/trade_proposals.evaluate_candidates).

Candidate generation falls back to the deterministic enumerator on any LLM
failure or empty/garbage output, so proposals keep working without the model.
"""
from __future__ import annotations

import logging

from backend.agents.base_agent import SONNET, BaseAgent, parse_json_output
from backend.services.trade.league_state import LeagueState
from backend.services.trade.trade_proposals import Candidate, enumerate_candidates
from backend.services.trade.value_engine import InSeasonValue

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You propose candidate fantasy-football trades for one team against the rest of
its league. Suggest plausible swaps that address a real positional need or
surplus — never random pairings. You are ONLY generating candidates; a
deterministic value engine decides which are actually good, so propose a handful
of plausible ideas and let it judge.

Return ONLY a JSON array, each item:
  {"give": ["<my player id>", ...], "get": ["<their player id>", ...], "team_id": "<their team id>"}
Use the exact canonical player ids and team ids provided. No prose."""


def _format_roster(team, values) -> str:
    lines = [f"team_id={team.team_id} ({team.team_name}){' [ME]' if team.is_me else ''}:"]
    for rp in team.roster:
        v = values.get(rp.canonical_player_id)
        sig = (
            f"fv={v.forward_value},trend={v.value_trend.value},conf={v.confidence.value}"
            f"{',buy' if v.buy_low else ''}{',sell' if v.sell_high else ''}"
            if v else "no-value"
        )
        lines.append(f"  id={rp.canonical_player_id} {rp.name} ({rp.position}) [{sig}]")
    return "\n".join(lines)


def _parse_candidates(raw: str, state: LeagueState, my_team_id: str) -> list[Candidate]:
    """Parse + VALIDATE the LLM's candidates against real ids; drop anything that
    doesn't resolve (so the model can't smuggle in bogus players)."""
    my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
    if my_team is None:
        return []
    my_ids = {rp.canonical_player_id for rp in my_team.roster}
    team_ids = {t.team_id for t in state.teams}
    other_ids = {
        rp.canonical_player_id for t in state.teams if t.team_id != my_team_id
        for rp in t.roster
    }

    try:
        data = parse_json_output(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []

    out: list[Candidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        give = [g for g in item.get("give", []) if g in my_ids]
        get = [g for g in item.get("get", []) if g in other_ids]
        team_id = item.get("team_id")
        if give and get and team_id in team_ids and team_id != my_team_id:
            out.append(Candidate(tuple(give), tuple(get), team_id))
    return out


class TradeProposalsAgent(BaseAgent):
    AGENT_NAME = "trade_proposals"
    AGENT_MODEL = SONNET
    AGENT_MAX_TOKENS = 800

    async def generate_candidates(
        self,
        state: LeagueState,
        my_team_id: str,
        values: dict[str, InSeasonValue],
    ) -> list[Candidate]:
        """LLM candidate generation with a deterministic enumerator fallback."""
        my_team = next((t for t in state.teams if t.team_id == my_team_id), None)
        others = [t for t in state.teams if t.team_id != my_team_id]
        if my_team is None or not others:
            return []

        user = "\n\n".join([
            "MY TEAM:", _format_roster(my_team, values),
            "OTHER TEAMS:", *[_format_roster(t, values) for t in others],
            "Propose plausible candidate trades as the JSON array described.",
        ])
        input_data = {
            "my_team_id": my_team_id,
            "rosters": {
                t.team_id: [rp.canonical_player_id for rp in t.roster]
                for t in state.teams
            },
        }
        try:
            raw = await self.call_once(
                system=_SYSTEM_PROMPT, user=user, input_data=input_data,
                entity_id=my_team_id,
            )
            candidates = _parse_candidates(raw, state, my_team_id)
            if candidates:
                return candidates
        except Exception as exc:
            logger.warning("trade candidate generation failed: %s", exc)

        return enumerate_candidates(state, values, my_team_id)
