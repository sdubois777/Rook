"""
Waiver recommendation engine — a waiver add/drop as a ONE-SIDED trade.

REUSE, don't rebuild: the net value of "add A, drop D" is the change in your
optimal STARTING lineup's points/week on the resulting roster — the SAME objective
the trade verdict uses. We import and COMPOSE the pure trade primitives and never
edit them:
  gain(A, D) = lineup_strength_ppg(fit_to_limit(roster - D + A, limit), rules, repl)
             - lineup_strength_ppg(roster, rules, repl)

For each pool add we find the drop D* that maximizes gain (fit_to_limit auto-drops
the marginal player when the roster is at its limit; below the limit an add needs
no drop — an open slot). Adds are ranked by real-ppw gain, with a transparent
position-need / fresh-news ordering nudge (the displayed gain stays honest).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.services.trade.league_state import RosterPlayer, TeamState
from backend.services.trade.lineup import (
    DEFAULT_LINEUP_RULES,
    LineupPlayer,
    LineupRules,
    fit_to_limit,
    lineup_strength_ppg,
)
from backend.services.trade.trade_proposals import analyze_roster
from backend.services.trade.value_engine import (
    InSeasonValue,
    replacement_ppg_by_position,
)
from backend.services.waiver.faab import FaabSuggestion, suggest_bid
from backend.services.waiver.news_tiein import DIRECT_POSITIVE_TYPES, NewsInfo

# Ordering nudges ONLY (the displayed lineup gain is never altered). A need-
# position add and a fresh-news add float up among near-equal gains.
NEED_RANK_BONUS = 0.5      # ppw-equivalent, ranking only
NEWS_RANK_BONUS = 0.5      # ppw-equivalent, ranking only
MAX_RESULTS = 25

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DropInfo:
    id: str
    name: str
    position: str
    forward_value: float


@dataclass(frozen=True)
class Recommendation:
    add: InSeasonValue
    add_nfl_team: Optional[str]
    drop: Optional[DropInfo]          # None → open roster slot, no drop needed
    lineup_delta_ppw: float           # the honest objective
    fills_need: bool
    need_positions: tuple[str, ...]
    faab: FaabSuggestion
    news: Optional[NewsInfo]
    why: str
    _rank_score: float                # ordering only (not serialized to the client)


def _lp(rp: RosterPlayer, values: dict[str, InSeasonValue]) -> Optional[LineupPlayer]:
    v = values.get(rp.canonical_player_id)
    if v is None:
        return None
    return LineupPlayer(rp.canonical_player_id, rp.position, v.forward_value, v.forward_ppg)


def recommend(
    acting_team: TeamState,
    pool: list[RosterPlayer],
    values: dict[str, InSeasonValue],
    *,
    rules: Optional[LineupRules] = None,
    roster_limit: int,
    faab_remaining: int,
    news_map: Optional[dict[str, NewsInfo]] = None,
    max_results: int = MAX_RESULTS,
) -> list[Recommendation]:
    """Rank the available pool by the real-ppw lineup improvement each add makes to
    ``acting_team``, paired with the best drop. Pure — no DB, no LLM."""
    rules = rules or DEFAULT_LINEUP_RULES
    news_map = news_map or {}

    roster = [lp for lp in (_lp(rp, values) for rp in acting_team.roster) if lp is not None]
    name_by_id = {rp.canonical_player_id: rp.name for rp in acting_team.roster}
    repl = replacement_ppg_by_position(values)
    base = lineup_strength_ppg(roster, rules, repl)
    under_limit = len(roster) < roster_limit

    needs = analyze_roster(acting_team, values, rules).needs

    recs: list[Recommendation] = []
    for rp in pool:
        v = values.get(rp.canonical_player_id)
        if v is None:
            continue
        add_lp = LineupPlayer(rp.canonical_player_id, rp.position, v.forward_value, v.forward_ppg)

        drop_lp, gain = _best_add_drop(roster, add_lp, base, rules, repl, roster_limit, under_limit)
        drop = DropInfo(
            drop_lp.player_id, name_by_id.get(drop_lp.player_id, ""),
            drop_lp.position, drop_lp.forward_value,
        ) if drop_lp else None

        news = news_map.get(rp.canonical_player_id)
        # Depth-chart "next up" opportunity picks are an OFFENSE handcuff concept; a
        # K/DST has no next-man-up (the depth-chart map is skill-only), so this can't
        # normally fire — but loud-warn + drop it if it ever does, rather than
        # surfacing a nonsensical "DST opportunity" (K/DEF streaming arc, slice 3).
        if news is not None and news.kind == "opportunity" and rp.position in ("K", "DEF"):
            logger.warning(
                "waiver: dropped a K/DST depth-chart opportunity pick for %s (%s) — "
                "K/DST have no handcuff next-up", rp.name, rp.position,
            )
            news = None
        # Include an add if it improves the lineup OR carries a fresh signal worth a stash.
        if gain < 0.01 and news is None:
            continue

        fills_need = rp.position in needs
        has_bump = news is not None and (
            news.kind == "opportunity" or news.signal_type in DIRECT_POSITIVE_TYPES
        )
        faab = suggest_bid(
            gain_ppw=gain,
            faab_remaining=faab_remaining,
            value_over_replacement=v.forward_ppg - repl.get(rp.position, 0.0),
            replacement_ppg=repl.get(rp.position, 0.0),
            has_news_bump=has_bump,
        )

        rank = gain + (NEED_RANK_BONUS if fills_need else 0.0) + (NEWS_RANK_BONUS if news else 0.0)
        recs.append(Recommendation(
            add=v, add_nfl_team=rp.nfl_team, drop=drop,
            lineup_delta_ppw=round(gain, 2), fills_need=fills_need,
            need_positions=tuple(sorted(needs)), faab=faab, news=news,
            why=_why(v, gain, fills_need, news), _rank_score=rank,
        ))

    recs.sort(key=lambda r: (-r._rank_score, r.add.canonical_player_id))
    return recs[:max_results]


def best_add(
    acting_team: TeamState,
    pool: list[RosterPlayer],
    values: dict[str, InSeasonValue],
    *,
    rules: Optional[LineupRules] = None,
    roster_limit: int,
) -> Optional[tuple[RosterPlayer, float]]:
    """The single highest-gain pool add IGNORING the recommend() threshold — the
    'near-miss' for the silence state ('nothing worth claiming — closest is…')."""
    rules = rules or DEFAULT_LINEUP_RULES
    roster = [lp for lp in (_lp(rp, values) for rp in acting_team.roster) if lp is not None]
    repl = replacement_ppg_by_position(values)
    base = lineup_strength_ppg(roster, rules, repl)
    under_limit = len(roster) < roster_limit
    best: Optional[tuple[RosterPlayer, float]] = None
    for rp in pool:
        v = values.get(rp.canonical_player_id)
        if v is None:
            continue
        add_lp = LineupPlayer(rp.canonical_player_id, rp.position, v.forward_value, v.forward_ppg)
        _drop, gain = _best_add_drop(roster, add_lp, base, rules, repl, roster_limit, under_limit)
        if best is None or gain > best[1]:
            best = (rp, gain)
    return best


def _best_add_drop(
    roster: list[LineupPlayer],
    add_lp: LineupPlayer,
    base: float,
    rules: LineupRules,
    repl: dict[str, float],
    roster_limit: int,
    under_limit: bool,
) -> tuple[Optional[LineupPlayer], float]:
    """Best (drop, gain) for adding ``add_lp``. Below the roster limit an add needs
    no drop (open slot → drop=None). At the limit, pick the drop that maximizes the
    resulting lineup ppw, tie-broken toward dropping the LOWEST-value player."""
    if under_limit:
        post = fit_to_limit(roster + [add_lp], roster_limit)
        return None, lineup_strength_ppg(post, rules, repl) - base

    best_drop: Optional[LineupPlayer] = None
    best_gain = float("-inf")
    for d in roster:
        post = fit_to_limit([p for p in roster if p.player_id != d.player_id] + [add_lp], roster_limit)
        g = lineup_strength_ppg(post, rules, repl) - base
        if g > best_gain or (g == best_gain and best_drop is not None and d.forward_value < best_drop.forward_value):
            best_gain, best_drop = g, d
    return best_drop, best_gain


def _why(v: InSeasonValue, gain: float, fills_need: bool, news: Optional[NewsInfo]) -> str:
    bits = [f"+{gain:.1f} ppw to your starting lineup" if gain >= 0.01 else "depth/stash"]
    if fills_need:
        bits.append(f"fills a need at {v.position}")
    if news and news.kind == "opportunity":
        who = f" ({news.starter_name})" if news.starter_name else ""
        bits.append(f"next up if{who} the starter is out")
    elif news:
        bits.append("fresh news signal")
    return "; ".join(bits)
