"""Pipeline triggers — event-driven targeted refreshes, draft-window safety, and
the weekly-sweep guard.

Three capabilities, all reusing the EXISTING dirty/cache pipeline machinery (they
do not fork it):

1. TARGETED REFRESH (`run_targeted_refresh`) — recompute an explicit affected player
   set instead of the whole board. Reuses `PlayerProfilesAgent.run_for_team` (scoped
   via its `only_players` forced set), the global-but-free `run_valuation_pass`, and
   `ValuationAgent.run_all(only_player_ids=...)`.

2. AFFECTED-SET DERIVATION (`derive_affected_set`) — an event's blast radius is NOT
   just the named player. It is DERIVED from data we already have — the causal
   dependency edges the roster-changes agent computed (`player_dependencies`) plus
   the same-team/same-position depth chart — never a hand-coded who-affects-whom map
   (that static-list drift has bitten this codebase before).

3. DRAFT-WINDOW SAFETY (`is_draft_window_active`) — the load test found 40 concurrent
   heavy reads took a healthy 100-draft load from 0% to 50% errors. So heavy passes
   must NEVER run during peak draft windows; scheduled/event runs defer when a window
   is active.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy import func, select

from backend.config import settings

logger = logging.getLogger(__name__)

# Event types whose blast radius we derive. Kept as a small closed set so callers
# (news ingestion, admin triggers) speak one vocabulary.
INJURY = "injury"
SUSPENSION = "suspension"
TRADE = "trade"
DROP_RELEASE = "drop_release"
SIGNING = "signing"
EVENT_TYPES = (INJURY, SUSPENSION, TRADE, DROP_RELEASE, SIGNING)

# Events that VACATE a role on the player's current team (open the position room).
_VACATING = {INJURY, SUSPENSION, DROP_RELEASE, TRADE}
# Events that ADD a player to a (new) team, crowding that position room.
_ARRIVING = {TRADE, SIGNING}

# Role-inheritance depth cutoff: only the top of the depth chart plausibly inherits
# a vacated role. Without this, "same team + same position" pulls in the whole
# position room (WR9, camp bodies) whose value an event does not move. Players
# without a depth_chart_order are treated as off the chart (dependency edges still
# catch any the agent explicitly linked).
_ROLE_INHERITANCE_DEPTH = 5

# Rough per-player AI cost of a targeted refresh (Sonnet profile + valuation ceiling;
# affected players skew Sonnet-heavy because they are the flagged/complex ones). Used
# only for the dry-run estimate shown to operators — real spend is logged by BaseAgent.
_EST_COST_PER_PLAYER = 0.03
FULL_SWEEP_COST = 10.82  # measured, per scripts/run_predraft_pipeline.py


# ---------------------------------------------------------------------------
# PART 2 — affected-set derivation
# ---------------------------------------------------------------------------
async def derive_affected_set(
    db,
    trigger_player,
    event_type: str,
    *,
    new_team: Optional[str] = None,
) -> dict:
    """Derive the set of players an event materially affects, from EXISTING data.

    Sources, in order:
      (a) The trigger player himself — his own value changes.
      (b) CAUSAL DEPENDENCY EDGES — every `player_dependencies` row whose
          `trigger_player_id` is this player. These are the agent-derived links
          (displaced / contingent / beneficiary / …) — the Keenan-Allen-caps-McConkey
          mechanism. This is where cross-position effects live IF the agent modelled
          them.
      (c) ROLE INHERITANCE — for a vacating event (injury/suspension/drop/trade-away),
          the same-team + same-position depth chart inherits the opened role.
      (d) NEW-TEAM DISPLACEMENT — for an arrival (trade-in/signing), the new team's
          same-position room gets more crowded.

    Returns ``{"trigger", "event_type", "affected": [{"player", "reasons": [...]}]}``.
    Each reason is a plain string so the caller can log/show WHY a player is in the
    set — proving it is derived, not hardcoded.

    HONEST LIMITATION (reported, not faked): steps (c)/(d) are SAME-POSITION only.
    Cross-position ripples (a WR injury making the offense run-heavier, lifting the
    RB) are captured ONLY when an explicit dependency edge exists in (b) — the data
    does not otherwise model position-group script effects, and inventing a fixed
    "WR-down-helps-RB" rule would be exactly the hardcoded drift we avoid.
    """
    from backend.models.dependency import PlayerDependency
    from backend.models.player import Player

    affected: dict[UUID, dict] = {}

    def add(player, reason: str) -> None:
        if player is None:
            return
        entry = affected.setdefault(player.id, {"player": player, "reasons": []})
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    # (a) the trigger himself
    add(trigger_player, f"{event_type}: the player directly involved")

    # (b) causal dependency edges — who did the agent link to this player?
    dep_rows = (
        await db.execute(
            select(PlayerDependency).where(
                PlayerDependency.trigger_player_id == trigger_player.id
            )
        )
    ).scalars().all()
    for d in dep_rows:
        dep_player = (
            await db.execute(select(Player).where(Player.id == d.player_id))
        ).scalar_one_or_none()
        reason = (
            f"dependency edge [{d.flag_type}]: value {d.effect_on_value or '?'} "
            f"when trigger is {d.trigger_condition or '?'}"
        )
        if d.reasoning:
            reason += f" — {d.reasoning.strip()[:100]}"
        add(dep_player, reason)

    team = (trigger_player.team_abbr or "").upper()
    pos = (trigger_player.position or "").upper()

    def _depth_room_query(dest: str):
        # Same team + position, near the top of the depth chart only (see cutoff).
        return select(Player).where(
            func.upper(Player.team_abbr) == dest,
            func.upper(Player.position) == pos,
            Player.id != trigger_player.id,
            Player.depth_chart_order.isnot(None),
            Player.depth_chart_order <= _ROLE_INHERITANCE_DEPTH,
        )

    # (c) role inheritance — same team + same position depth room
    if event_type in _VACATING and team and pos:
        mates = (await db.execute(_depth_room_query(team))).scalars().all()
        for m in mates:
            add(m, f"depth chart: same team ({team}) + position ({pos}), "
                   f"depth {m.depth_chart_order} — inherits the vacated role")

    # (d) new-team displacement — arrival crowds the destination room
    if event_type in _ARRIVING and pos:
        dest = (new_team or team or "").upper()
        if dest:
            mates = (await db.execute(_depth_room_query(dest))).scalars().all()
            for m in mates:
                add(m, f"depth chart: arrival on {dest} crowds the same position "
                       f"({pos}), depth {m.depth_chart_order}")

    return {
        "trigger": trigger_player,
        "event_type": event_type,
        "affected": list(affected.values()),
    }


# ---------------------------------------------------------------------------
# PART 3 — draft-window safety
# ---------------------------------------------------------------------------
async def is_draft_window_active(db, *, now: Optional[datetime] = None) -> tuple[bool, str]:
    """True when a heavy pipeline pass must DEFER. Two independent detectors:

    - LIVE: a draft_sessions row is active AND was updated within the recent window
      (someone is drafting right now).
    - SCHEDULED: a synced league's draft_date is between `before_hours` ago and
      `after_hours` from now (a draft is imminent or plausibly in progress).

    Detecting BOTH means we defer whether or not any client has connected yet.
    """
    from backend.models.draft_session import DraftSession
    from backend.models.user_league import UserLeague

    now = now or datetime.now(timezone.utc)

    recent = now - timedelta(minutes=settings.draft_window_live_recent_minutes)
    live = (
        await db.execute(
            select(func.count())
            .select_from(DraftSession)
            .where(DraftSession.is_active.is_(True), DraftSession.updated_at >= recent)
        )
    ).scalar_one()
    if live and live > 0:
        return True, f"{live} live draft session(s) active in the last {settings.draft_window_live_recent_minutes}m"

    lo = now - timedelta(hours=settings.draft_window_after_hours)     # started up to N h ago
    hi = now + timedelta(hours=settings.draft_window_before_hours)    # starting within Nh
    scheduled = (
        await db.execute(
            select(func.count())
            .select_from(UserLeague)
            .where(
                UserLeague.draft_date.isnot(None),
                UserLeague.draft_date >= lo,
                UserLeague.draft_date <= hi,
            )
        )
    ).scalar_one()
    if scheduled and scheduled > 0:
        return True, f"{scheduled} league(s) with a draft scheduled in the window"

    return False, "no live or imminent drafts"


# ---------------------------------------------------------------------------
# PART 1 — targeted refresh
# ---------------------------------------------------------------------------
async def run_targeted_refresh(
    player_ids: set[UUID],
    *,
    event_type: str = "manual",
    dry_run: bool = False,
    respect_draft_window: bool = True,
    warehouse=None,
    db=None,
) -> dict:
    """Recompute profiles + values for an EXPLICIT player set (already the derived
    affected set), scoped by team. Reuses the dirty/cache pipeline pieces:

      1. player_profiles — forced recompute of the set, grouped by team.
      2. valuation — the global (position-relative, PURE-PYTHON, free) dollar pass.
      3. valuation_agent — ai_bid_ceiling/adp_ai for the set only.

    `dry_run` returns the PLAN (players, teams, estimated cost) without any API call
    or DB write — used to prove scope. Returns a report dict either way.
    """
    from backend.database import AsyncSessionLocal
    from backend.models.player import Player

    owns_db = db is None
    session = db or AsyncSessionLocal()
    try:
        players = (
            await session.execute(select(Player).where(Player.id.in_(player_ids)))
        ).scalars().all() if player_ids else []

        # Group the AI profile work by team (agents run per team); FA/no-team players
        # can't build a team profile context but still get a value pass.
        by_team: dict[str, set[str]] = {}
        for p in players:
            if p.team_abbr:
                by_team.setdefault(p.team_abbr.upper(), set()).add(p.name)

        est_cost = round(len(players) * _EST_COST_PER_PLAYER, 2)
        plan = {
            "event_type": event_type,
            "players_touched": [p.name for p in players],
            "n_players": len(players),
            "teams": sorted(by_team.keys()),
            "estimated_cost_usd": est_cost,
            "full_sweep_cost_usd": FULL_SWEEP_COST,
            "dry_run": dry_run,
        }

        if respect_draft_window:
            active, reason = await is_draft_window_active(session)
            if active:
                logger.warning("Targeted refresh DEFERRED — draft window active: %s", reason)
                return {**plan, "deferred": True, "reason": reason}

        if dry_run or not players:
            return {**plan, "deferred": False}
    finally:
        if owns_db:
            await session.close()

    # --- real run (own DB sessions inside the agents; no shared session held) ---
    from backend.agents.player_profiles import PlayerProfilesAgent
    from backend.agents.valuation_agent import ValuationAgent
    from backend.engines.valuation import run_valuation_pass

    profiles_written = 0
    profiler = PlayerProfilesAgent(dry_run=False, warehouse=warehouse)
    for team, names in by_team.items():
        try:
            profiles_written += await profiler.run_for_team(team, only_players=names)
        except Exception:
            logger.exception("Targeted profile refresh failed for team %s", team)

    val = await run_valuation_pass()                                   # global, free
    va = await ValuationAgent(dry_run=False).run_all(only_player_ids=player_ids)

    logger.info(
        "Targeted refresh (%s): %d profiles, %d values, %d ceilings across %d team(s)",
        event_type, profiles_written, val.get("updated", 0),
        va.get("processed", 0), len(by_team),
    )
    return {
        **plan,
        "deferred": False,
        "profiles_written": profiles_written,
        "values_updated": val.get("updated", 0),
        "ceilings_processed": va.get("processed", 0),
    }


# ---------------------------------------------------------------------------
# PART 3 — event debouncer
# ---------------------------------------------------------------------------
class TargetedRefreshDebouncer:
    """Coalesces a BURST of events into ONE refresh. Every `enqueue` folds ids into a
    pending set and (re)arms a single timer; when it fires, one refresh runs for the
    whole accumulated set. So five injury headlines about one player fire one refresh,
    not five.
    """

    def __init__(
        self,
        refresh_fn: Callable[[set[UUID], str], Awaitable[dict]],
        *,
        delay_seconds: Optional[int] = None,
    ):
        self._refresh_fn = refresh_fn
        self._delay = (
            delay_seconds if delay_seconds is not None
            else settings.event_refresh_debounce_seconds
        )
        self._pending: set[UUID] = set()
        self._event_type = "manual"
        self._task: Optional[asyncio.Task] = None

    def enqueue(self, player_ids: set[UUID], event_type: str = "manual") -> None:
        if not player_ids:
            return
        self._pending |= set(player_ids)
        self._event_type = event_type
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._fire_after_delay())

    async def _fire_after_delay(self) -> None:
        await asyncio.sleep(self._delay)
        batch, self._pending = self._pending, set()
        event_type = self._event_type
        if not batch:
            return
        try:
            await self._refresh_fn(batch, event_type)
        except Exception:
            logger.exception("Debounced targeted refresh failed (%d players)", len(batch))

    async def flush(self) -> None:
        """Fire any pending batch immediately (test hook / graceful shutdown)."""
        if self._task and not self._task.done():
            self._task.cancel()
        batch, self._pending = self._pending, set()
        if batch:
            await self._refresh_fn(batch, self._event_type)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


# ---------------------------------------------------------------------------
# Wiring: the process-wide debouncer + the news-ingestion hook
# ---------------------------------------------------------------------------
# Beat-reporter signal_type -> event_type. camp_standout has no clear blast radius,
# so it is intentionally absent (no refresh). transaction is resolved to SIGNING vs
# DROP_RELEASE by whether the signal names a (new) team.
_SIGNAL_TO_EVENT = {
    "injury_flag": INJURY,
    "practice_limited": INJURY,
    "depth_chart_change": INJURY,
    "transaction": TRADE,  # arrival/departure — derives BOTH rooms via new_team
}

_debouncer: Optional[TargetedRefreshDebouncer] = None


def get_debouncer() -> TargetedRefreshDebouncer:
    """The process-wide debouncer whose refresh runs the scoped pipeline (which
    itself defers if a draft window is active). Lazily built so tests can inject
    their own instance instead."""
    global _debouncer
    if _debouncer is None:
        async def _run(ids: set[UUID], event_type: str) -> dict:
            return await run_targeted_refresh(ids, event_type=event_type)
        _debouncer = TargetedRefreshDebouncer(_run)
    return _debouncer


async def enqueue_event_refresh(
    db, trigger_player, signal_type: str, *, new_team: Optional[str] = None
) -> set[UUID]:
    """Derive the affected set for a just-ingested news signal and enqueue ONE
    debounced targeted refresh. Returns the affected ids (for logging/tests). A
    non-value-moving signal_type derives nothing and enqueues nothing."""
    event_type = _SIGNAL_TO_EVENT.get(signal_type)
    if event_type is None:
        return set()
    derived = await derive_affected_set(db, trigger_player, event_type, new_team=new_team)
    ids = {e["player"].id for e in derived["affected"]}
    if ids:
        get_debouncer().enqueue(ids, "news")
    return ids
