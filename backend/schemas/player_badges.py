"""
Shared badge fields for player-out response schemas.

Position and injury are things that "should be everywhere a player is shown". To
stop the N-edit pattern (position was scattered; injury was added to 4 of ~13
schemas one at a time), every player-out schema that feeds the shared frontend
``PlayerName`` primitive inherits this mixin, so the NEXT badge field is added
ONCE here, not N times across routers.

All fields are additive + Optional — no existing consumer breaks, and a healthy
player serializes ``injury_status=None`` (the frontend renders no badge).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class PlayerBadgeFields(BaseModel):
    """Mixin for player-out schemas: the badge fields the shared FE primitive reads.

    ``injury_status`` is the canonical live code ("Q" | "D" | "O" | "IR"; None =
    healthy) from ``backend/utils/injury_status.py``, sourced from
    ``Player.injury_status``. Display-only — NOT a valuation input.
    """
    injury_status: Optional[str] = None
