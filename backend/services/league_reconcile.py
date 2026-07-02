"""
LeagueReconciler — tier-cap reconciliation for a user's current-season leagues.

Policy (locked):
- The cap counts ONLY active (current-season, non-suspended) leagues. Finished
  (past-season) leagues never count and are never touched.
- On a tier DROP that leaves active_count > cap: do NOTHING automatically — the
  account enters a COMPUTED over-limit "must choose" state (active_count > cap).
  The user then picks which stay; the rest are parked (suspended), never deleted.
- On a tier RISE (or unlimited): restore parked leagues up to the new cap,
  longest-parked first. A partial upgrade that still can't fit everything leaves
  the remainder parked (over-limit persists until resolved).

No method commits — the caller (webhook / request) owns the transaction. Tier is
still written solely by the webhook; this only reconciles leagues at that point.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from backend.core.exceptions import ValidationError
from backend.models.user import TIER_LIMITS


def _cap(tier: str) -> int | None:
    """max_leagues for a tier (None = unlimited)."""
    return TIER_LIMITS.get(tier, {}).get("max_leagues")


class LeagueReconciler:
    def __init__(self, league_repo):
        self._repo = league_repo

    async def reconcile_for_tier(self, user_id: uuid.UUID, new_tier: str) -> None:
        """Restore parked leagues up to the new cap. Never auto-parks — a drop
        that leaves active > cap just leaves the computed over-limit state."""
        cap = _cap(new_tier)
        suspended = await self._repo.get_suspended_leagues(user_id)
        if not suspended:
            return
        if cap is None:  # unlimited — restore everything
            await self._repo.set_suspended(
                user_id, [lg.id for lg in suspended], None
            )
            return
        active = await self._repo.get_active_leagues(user_id)
        room = cap - len(active)
        if room > 0:
            restore = [lg.id for lg in suspended[:room]]  # longest-parked first
            await self._repo.set_suspended(user_id, restore, None)

    async def limit_state(self, user_id: uuid.UUID, tier: str) -> dict:
        """Over-limit snapshot + the chooser candidate set (current-season only)."""
        cap = _cap(tier)
        candidates = await self._repo.get_current_season_leagues(user_id)
        active_count = sum(1 for lg in candidates if lg.suspended_at is None)
        return {
            "max_leagues": cap,
            "active_count": active_count,
            "over_limit": cap is not None and active_count > cap,
            "candidates": candidates,
        }

    async def resolve_keep(
        self, user_id: uuid.UUID, tier: str, keep_ids: list[uuid.UUID]
    ) -> None:
        """Chooser: keep <= cap current-season leagues active, park the rest.
        Idempotent; allows swapping which are parked. Raises on keep > cap or ids
        outside the user's current-season set. No commit."""
        cap = _cap(tier)
        if cap is not None and len(keep_ids) > cap:
            raise ValidationError(
                f"Can keep at most {cap} active leagues on this plan "
                f"(tried to keep {len(keep_ids)})"
            )
        candidates = await self._repo.get_current_season_leagues(user_id)
        candidate_ids = {lg.id for lg in candidates}
        keep_set = set(keep_ids)
        unknown = keep_set - candidate_ids
        if unknown:
            raise ValidationError(
                "Cannot keep leagues that aren't current-season leagues you own"
            )
        now = datetime.now(timezone.utc)
        to_keep = [lg.id for lg in candidates if lg.id in keep_set]
        to_park = [lg.id for lg in candidates if lg.id not in keep_set]
        await self._repo.set_suspended(user_id, to_keep, None)
        await self._repo.set_suspended(user_id, to_park, now)
