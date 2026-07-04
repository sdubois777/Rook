"""
Sleeper draft-state backfill — full recovery via the PUBLIC Sleeper REST API.

The live capture path (WS frame interception) is delta-only: any pick that
streams while the extension is dead/orphaned is lost, and nothing in the frames
can recover it. Sleeper, uniquely, publishes the ENTIRE draft state on a free,
unauthenticated API:

    GET https://api.sleeper.app/v1/draft/{draft_id}         → type/status/
        draft_order {user_id: slot} / settings (teams, budget, reversal_round)
    GET https://api.sleeper.app/v1/draft/{draft_id}/picks   → EVERY pick:
        pick_no, round, draft_slot, player_id (Sleeper id → exact canonical
        resolution), picked_by (user_id — EMPTY for autopicks/mock bots),
        metadata {first_name, last_name, position, team, amount (auction), slot}

So the extension periodically posts a `draft_sync` event carrying the draft_id +
the viewer's Sleeper user_id, and the backend reconciles: fetch everything,
record any pick the engine doesn't have (idempotent), broadcast the recovered
picks to the UI. A dead WS becomes a latency downgrade instead of a total loss,
and a page/extension reload fully re-syncs. Shapes verified against real
captured mock drafts (snake + auction).

Pure event-building is separated from fetching for unit tests.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

SLEEPER_API = "https://api.sleeper.app/v1"
_TIMEOUT = 10.0


async def fetch_draft(draft_id: str) -> Optional[dict]:
    """Draft metadata (type/status/draft_order/settings), or None on any failure."""
    return await _get(f"{SLEEPER_API}/draft/{draft_id}")


async def fetch_picks(draft_id: str) -> Optional[list]:
    """All picks made so far, or None on any failure (an empty draft returns [])."""
    return await _get(f"{SLEEPER_API}/draft/{draft_id}/picks")


async def _get(url: str) -> Any:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data  # Sleeper returns JSON null for unknown ids → None
    except Exception as exc:
        logger.warning("Sleeper backfill fetch failed (%s): %s", url, exc)
        return None


def my_slot(draft: dict, my_user_id: str | None) -> Optional[int]:
    """The viewer's serpentine slot from draft_order {user_id: slot}."""
    if not my_user_id:
        return None
    order = draft.get("draft_order") or {}
    slot = order.get(str(my_user_id))
    return int(slot) if slot is not None else None


def build_pick_events(
    draft: dict, picks: list, my_user_id: str | None
) -> list[dict]:
    """Normalize REST picks into the SAME event payloads the live resolvers emit
    (snake_pick / draft_pick), so the existing record + broadcast paths handle
    them verbatim.

    is_yours: picked_by == my_user_id when present, else draft_slot == my slot —
    the slot fallback is ESSENTIAL: picked_by is empty for autopicks and mock
    bots (verified in real captures). Team labels mirror the live resolvers
    ("Team N", the viewer's own slot labeled "You").
    """
    draft_type = draft.get("type")
    is_auction = draft_type == "auction"
    slot_of_me = my_slot(draft, my_user_id)

    events: list[dict] = []
    for p in sorted(picks or [], key=lambda x: x.get("pick_no") or 0):
        md = p.get("metadata") or {}
        name = f"{md.get('first_name') or ''} {md.get('last_name') or ''}".strip() or None
        slot = p.get("draft_slot")
        slot = int(slot) if slot is not None else None
        picked_by = p.get("picked_by") or ""
        mine = (
            (picked_by and my_user_id and str(picked_by) == str(my_user_id))
            or (slot is not None and slot_of_me is not None and slot == slot_of_me)
        )
        team_label = "You" if mine else (f"Team {slot}" if slot is not None else "Unknown")

        if is_auction:
            amount = md.get("amount")
            try:
                price = int(amount) if amount is not None else 0
            except (TypeError, ValueError):
                price = 0
            events.append({
                "type": "draft_pick",
                "payload": {
                    "player_name": name,
                    "player_id": "",
                    "sleeper_player_id": p.get("player_id") or None,
                    "position": md.get("position") or None,
                    "pro_team": md.get("team") or None,
                    "final_price": price,
                    "winner": team_label,
                    "is_yours": bool(mine),
                },
            })
        else:
            events.append({
                "type": "snake_pick",
                "payload": {
                    "pick_number": p.get("pick_no"),
                    "player_name": name,
                    "position": md.get("position") or None,
                    "nfl_team": md.get("team") or None,
                    "sleeper_player_id": p.get("player_id") or None,
                    "picker": team_label,
                    "is_yours": bool(mine),
                    "round": p.get("round"),
                },
            })
    return events
