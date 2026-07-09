"""
Deterministic pre-draft availability pass — computes + persists the games-missed
availability discount (engines/availability.py) for every player from Sleeper's
STRUCTURED status. Runs as the LAST pre-draft pipeline step. NO Sonnet.

Base value fields (ai_bid_ceiling / recommended_bid_ceiling / projected_ppr_season)
are UNTOUCHED — the draft-ranked value reads ``base × availability_factor`` at
read time (draftboard / live-draft engine). This keeps the pass fully IDEMPOTENT:
each run recomputes availability_factor from the current Sleeper source and resets
healthy players to 1.000, so re-running never compounds a discount.

Structured source: the RAW Sleeper /players/nfl dump ``status`` field ("Physically
Unable to Perform" / "Injured Reserve" / "Suspended") — NOT fetch_sleeper_players
(which filters to Active/Inactive, dropping exactly the PUP/IR rows we need) and
NOT import_injuries (no PUP/IR/suspension designation, verified). Every unmapped
designation loud-warns via compute_availability; no silent discards.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import select

from backend.engines.availability import compute_availability, designation_from_sleeper
from backend.models.player import Player

logger = logging.getLogger(__name__)

_SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"


def _norm(v) -> str:
    return str(v).strip() if v is not None else ""


def fetch_sleeper_status_map() -> dict[str, tuple[Optional[str], Optional[str]]]:
    """{sleeper_id -> (status, injury_status)} from the RAW Sleeper dump — the
    structured PUP/IR/Suspended ``status`` that fetch_sleeper_players filters out."""
    resp = httpx.get(_SLEEPER_PLAYERS_URL, timeout=60)
    resp.raise_for_status()
    dump = resp.json()
    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for pid, p in dump.items():
        sid = _norm(pid)
        if sid:
            out[sid] = (p.get("status"), p.get("injury_status"))
    return out


async def apply_availability_discounts(
    db,
    *,
    status_by_sleeper_id: Optional[dict[str, tuple[Optional[str], Optional[str]]]] = None,
) -> dict:
    """Compute + persist availability_factor / games_missed / reason for every player.
    ``status_by_sleeper_id`` is injectable (tests); fetched from Sleeper otherwise.
    Idempotent: healthy players reset to 1.000/0/None. Returns a summary."""
    if status_by_sleeper_id is None:
        status_by_sleeper_id = fetch_sleeper_status_map()

    players = (await db.execute(select(Player))).scalars().all()
    discounted = updated = 0
    discounts: list[str] = []
    for p in players:
        status, inj = status_by_sleeper_id.get(_norm(p.sleeper_id), (None, None))
        designation = designation_from_sleeper(status, inj)
        result = compute_availability(designation)

        new_factor = Decimal(str(result.factor)).quantize(Decimal("1.000"))
        reason = result.reason or None
        if (p.availability_factor != new_factor
                or (p.availability_games_missed or 0) != result.games_missed
                or (p.availability_reason or None) != reason):
            updated += 1
        p.availability_factor = new_factor
        p.availability_games_missed = result.games_missed
        p.availability_reason = reason

        if result.factor < 1.0:
            discounted += 1
            discounts.append(f"{p.name} ({p.position}): {result.reason}")

    await db.commit()
    logger.info(
        "availability pass: %d discounted (of %d players), %d rows updated",
        discounted, len(players), updated,
    )
    for d in discounts[:25]:
        logger.info("  availability discount: %s", d)
    return {"discounted": discounted, "total": len(players), "updated": updated}
