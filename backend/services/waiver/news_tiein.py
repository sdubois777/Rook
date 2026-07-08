"""
Waiver news / breakout tie-in — true backup-surfacing.

Two ways a fresh beat-reporter signal makes a pool player interesting:

  DIRECT — the pool player himself carries a fresh signal (camp_standout,
  transaction, a role/injury note). Surface the headline on that player.

  OPPORTUNITY (backbone) — a STARTER carries a fresh opportunity-implying signal
  (injury_flag / depth_chart_change / practice_limited). The next-man-up is a
  direct depth-chart query (players.depth_chart_order, team+position); if that
  backup is in the available pool, surface HIM with the starter's signal.

The UNIVERSAL adjacency is depth_chart_order (populated by sync_rosters). The
CONTINGENT PlayerDependency flags (value_impact_pct + reasoning) are a PARTIAL
enrichment — they exist ~1:1 with displacement events (a competitor arrival),
NOT for every backup. We use depth_chart_order as the backbone and only ATTACH
CONTINGENT impact where it happens to exist. We never rely on CONTINGENT as the
sole adjacency source (that would silently miss most handcuffs).

BLURB CONSTRAINT: BeatReporterSignal.raw_text is the article TITLE only — no body
is stored (Thread 3 unbuilt). We show headline + signal_type + confidence. No
expand-for-body affordance (there is no body to reveal).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from backend.models.dependency import PlayerDependency
from backend.models.player import Player
from backend.repositories.news_repo import NewsRepository

logger = logging.getLogger(__name__)

# Signals on a STARTER that imply a backup opportunity.
OPPORTUNITY_TYPES = frozenset({"injury_flag", "depth_chart_change", "practice_limited"})
# Signals that, carried directly by a pool player, read as a positive/breakout note.
DIRECT_POSITIVE_TYPES = frozenset({"camp_standout", "transaction", "depth_chart_change"})

NEWS_LOOKBACK_DAYS = 30      # how far back a signal still counts as "fresh"
_SKILL = ("QB", "RB", "WR", "TE")


@dataclass(frozen=True)
class NewsInfo:
    kind: str                         # "direct" | "opportunity"
    headline: str                     # BeatReporterSignal.raw_text (article TITLE only)
    signal_type: str
    confidence: Optional[str]
    source: Optional[str]
    flagged_at: Optional[str]         # ISO
    starter_name: Optional[str] = None          # opportunity: whose absence opens it
    contingent_impact_pct: Optional[float] = None   # from a CONTINGENT flag, if any
    contingent_reasoning: Optional[str] = None


def _next_up(chain: list[tuple[int, str, str]], starter_id: str) -> Optional[tuple[str, str]]:
    """Given a team+position depth chain sorted by depth_chart_order, return the
    (id, name) of the player immediately BELOW ``starter_id`` — the next man up.
    None if the starter isn't in the chain or is already last."""
    ids = [c[1] for c in chain]
    if starter_id not in ids:
        return None
    idx = ids.index(starter_id)
    if idx + 1 >= len(chain):
        return None
    _, nid, nname = chain[idx + 1]
    return nid, nname


async def build_news_map(
    db,
    pool_ids: set[str],
    *,
    now: Optional[datetime] = None,
    lookback_days: int = NEWS_LOOKBACK_DAYS,
) -> dict[str, NewsInfo]:
    """Map pool_player_id -> NewsInfo for pool players made interesting by a fresh
    signal (direct or via backup-surfacing). Reads recent signals + the depth
    chart + CONTINGENT flags. Returns {} when nothing fresh applies."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - _timedelta(lookback_days)

    rows, _total = await NewsRepository(db).list_feed(cutoff=cutoff, per_page=500)
    if not rows:
        return {}

    news: dict[str, NewsInfo] = {}

    # 1. DIRECT — a POSITIVE signal on a pool player himself (newest-first: first wins).
    #    Polarity gate: only DIRECT_POSITIVE_TYPES (breakout/role-up) attach to the
    #    player's OWN add card. A self injury_flag / practice_limited is SUPPRESSIVE
    #    on his own card — attaching it would contradict recommending the add, and
    #    (because recommendations keys inclusion + the rank bonus off news presence)
    #    would wrongly rescue + up-rank an injured add. The SAME signal_type on a
    #    STARTER is still handled — as an opportunity for his backup — by path 2 below.
    for sig, _pname, _pteam, _ppos, _pinj in rows:
        pid = str(sig.player_id) if sig.player_id else None
        if pid and pid in pool_ids and pid not in news and sig.signal_type in DIRECT_POSITIVE_TYPES:
            news[pid] = NewsInfo(
                kind="direct", headline=sig.raw_text or "", signal_type=sig.signal_type,
                confidence=sig.confidence, source=sig.source,
                flagged_at=sig.flagged_at.isoformat() if sig.flagged_at else None,
            )

    # 2. OPPORTUNITY — a starter's opportunity signal → surface the next man up.
    opp = [
        (str(sig.player_id), sig, pteam, ppos)
        for sig, _pn, pteam, ppos, _pinj in rows
        if sig.signal_type in OPPORTUNITY_TYPES and sig.player_id and pteam and ppos
    ]
    if opp:
        starter_ids = {sid for sid, *_ in opp}
        depth = await _depth_chart_map(db)
        starter_names = await _names_for(db, starter_ids)
        contingents = await _contingent_map(db, starter_ids, pool_ids)
        used_contingent = 0
        for starter_id, sig, pteam, ppos in opp:
            chain = depth.get((pteam.upper(), ppos.upper()), [])
            nxt = _next_up(chain, starter_id)
            if not nxt:
                continue
            nb_id, nb_name = nxt
            if nb_id not in pool_ids or nb_id in news:
                continue  # not claimable, or already carries its own (direct) signal
            impact, reasoning = contingents.get((starter_id, nb_id), (None, None))
            if impact is not None:
                used_contingent += 1
            news[nb_id] = NewsInfo(
                kind="opportunity", headline=sig.raw_text or "", signal_type=sig.signal_type,
                confidence=sig.confidence, source=sig.source,
                flagged_at=sig.flagged_at.isoformat() if sig.flagged_at else None,
                starter_name=starter_names.get(starter_id),
                contingent_impact_pct=impact, contingent_reasoning=reasoning,
            )
        # Loud-warn if we ever leaned ONLY on CONTINGENT (we never should — the
        # depth chart is the backbone; CONTINGENT is enrichment only).
        opp_surfaced = sum(1 for n in news.values() if n.kind == "opportunity")
        logger.info(
            "waiver news: %d opportunity picks from depth_chart_order backbone; "
            "%d enriched with a CONTINGENT impact%%",
            opp_surfaced, used_contingent,
        )

    return news


async def _depth_chart_map(db) -> dict[tuple[str, str], list[tuple[int, str, str]]]:
    """{(team_abbr, position): [(depth_chart_order, player_id, name), ...] sorted}
    for skill players with a depth_chart_order. The universal handcuff backbone."""
    rows = (await db.execute(
        select(Player.team_abbr, Player.position, Player.depth_chart_order, Player.id, Player.name)
        .where(
            Player.depth_chart_order.isnot(None),
            Player.team_abbr.isnot(None),
            Player.position.in_(_SKILL),
        )
    )).all()
    out: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for team, pos, order, pid, name in rows:
        out.setdefault((team.upper(), pos.upper()), []).append((int(order), str(pid), name))
    for chain in out.values():
        chain.sort(key=lambda c: c[0])
    return out


async def _names_for(db, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    uids = _to_uuids(ids)
    rows = (await db.execute(select(Player.id, Player.name).where(Player.id.in_(uids)))).all()
    return {str(pid): name for pid, name in rows}


async def _contingent_map(
    db, starter_ids: set[str], pool_ids: set[str],
) -> dict[tuple[str, str], tuple[float, str]]:
    """{(trigger_starter_id, beneficiary_pool_id): (value_impact_pct, reasoning)} for
    CONTINGENT dependency flags linking a signaled starter to a pool backup. PARTIAL
    by design — only displacement-derived pairs have a flag."""
    if not starter_ids or not pool_ids:
        return {}
    trig = _to_uuids(starter_ids)
    ben = _to_uuids(pool_ids)
    rows = (await db.execute(
        select(
            PlayerDependency.trigger_player_id, PlayerDependency.player_id,
            PlayerDependency.value_impact_pct, PlayerDependency.reasoning,
        ).where(
            PlayerDependency.flag_type == "contingent",
            PlayerDependency.trigger_player_id.in_(trig),
            PlayerDependency.player_id.in_(ben),
        )
    )).all()
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for trigger_id, player_id, impact, reasoning in rows:
        out[(str(trigger_id), str(player_id))] = (
            float(impact) if impact is not None else None, reasoning or "",
        )
    return out


def _to_uuids(ids: set[str]) -> list[uuid.UUID]:
    """Canonical ids are Player UUIDs; skip any non-UUID id defensively."""
    out: list[uuid.UUID] = []
    for x in ids:
        try:
            out.append(uuid.UUID(str(x)))
        except (ValueError, AttributeError, TypeError):
            continue
    return out


def _timedelta(days: int):
    from datetime import timedelta
    return timedelta(days=days)
