"""
League bias analysis — positional bias aggregates and the biggest
opportunity/trap players, computed from each player's market context.

Pure computation: callers fetch the players, this module does the
math. Returned dicts match the response models in routers/league.py.
"""
from __future__ import annotations

from typing import Any

from backend.engines.valuation import get_market_context

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

# A league price this many dollars under/over FantasyPros qualifies a
# player as an opportunity (cheaper than market) or a trap (richer).
_BIAS_HIGHLIGHT_THRESHOLD = 5

# How many opportunity/trap players to surface.
_TOP_N = 5


def build_bias_analysis(
    players: list[Any],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Compute league-vs-market bias from players with league values.

    Returns (positional_biases, top_opportunities, top_traps) as lists
    of dicts shaped for the league router's response models.
    """
    if not players:
        return [], [], []

    player_contexts = [(p, get_market_context(p)) for p in players]

    pos_data: dict[str, dict] = {}
    for p, mctx in player_contexts:
        pos = p.position
        if pos not in pos_data:
            pos_data[pos] = {"league_sum": 0.0, "fp_sum": 0.0, "bias_sum": 0.0, "count": 0}
        league = float(mctx["market_value_league"]) if mctx["market_value_league"] is not None else 0
        fp = float(mctx["market_value_fantasypros"]) if mctx["market_value_fantasypros"] is not None else 0
        bias = float(mctx["league_bias"]) if mctx["league_bias"] is not None else 0
        pos_data[pos]["league_sum"] += league
        pos_data[pos]["fp_sum"] += fp
        pos_data[pos]["bias_sum"] += bias
        pos_data[pos]["count"] += 1

    positional_biases: list[dict] = []
    for pos in SKILL_POSITIONS:
        d = pos_data.get(pos)
        if not d or d["count"] == 0:
            continue
        positional_biases.append({
            "position": pos,
            "avg_league_price": round(d["league_sum"] / d["count"], 1),
            "avg_fp_price": round(d["fp_sum"] / d["count"], 1),
            "avg_bias": round(d["bias_sum"] / d["count"], 1),
            "player_count": d["count"],
        })

    with_bias = [(p, m) for p, m in player_contexts if m["league_bias"] is not None]

    def _to_bias_player(p: Any, mctx: dict) -> dict:
        return {
            "id": str(p.id),
            "name": p.name,
            "position": p.position,
            "injury_status": p.injury_status,
            "market_value_league": float(mctx["market_value_league"]) if mctx["market_value_league"] is not None else None,
            "market_value_fantasypros": float(mctx["market_value_fantasypros"]) if mctx["market_value_fantasypros"] is not None else None,
            "bias": float(mctx["league_bias"]),
            "bias_signal": mctx["league_bias_signal"],
        }

    sorted_opps = sorted(with_bias, key=lambda x: float(x[1]["league_bias"]))
    top_opportunities = [
        _to_bias_player(p, m)
        for p, m in sorted_opps[:_TOP_N]
        if float(m["league_bias"]) < -_BIAS_HIGHLIGHT_THRESHOLD
    ]

    sorted_traps = sorted(with_bias, key=lambda x: float(x[1]["league_bias"]), reverse=True)
    top_traps = [
        _to_bias_player(p, m)
        for p, m in sorted_traps[:_TOP_N]
        if float(m["league_bias"]) > _BIAS_HIGHLIGHT_THRESHOLD
    ]

    return positional_biases, top_opportunities, top_traps
