"""
TEST-ONLY demo league source for the WAIVER feature — FLAG-GATED scaffolding.

⚠️  TEARDOWN: mirrors the trade demo. Gated behind ``WAIVER_DEMO_MODE`` (env,
default FALSE). When false, ``maybe_demo_waiver_source`` returns None and nothing
here is reached on the prod path. Teardown deletes this file + the WAIVER_DEMO
branches + the waiver-demo tests. Grep ``WAIVER_DEMO`` / ``waiver_demo``.

It REUSES the trade demo's real 12-team 2025 rosters (``seed_demo_league`` /
``DEMO_ROSTERS``) so "my roster" is identical to the trade page's, then adds the
two things a waiver page needs that a trade page doesn't:

  * an AVAILABLE POOL — every skill player with in-season data who ISN'T on a
    roster (get_free_agents() is a stub returning [] on all 3 platforms, so the
    pool MUST be seeded). Value comes from the SAME #149 per-week usage layer.
  * faked WAIVER SETTINGS — waiver_type="faab" + a budget + per-team remaining.
    None of this is persisted anywhere today (UserLeague has no waiver columns);
    real wiring is an explicit follow-up, out of v1 scope.

CONSISTENCY: my roster + the pool are valued in ONE ``evaluate_league`` call over
an augmented LeagueState (the 12 teams + a synthetic pool team), so every
forward_value / forward_ppg is on the same anchor basis — required for the
add/drop lineup math to be valid. (This basis includes the pool, so numbers can
differ slightly from the 12-team-only trade page; the waiver page is
self-consistent, which is what the objective needs.)

DEMO CAVEAT (loud): the roster/week is a pinned 2025 snapshot but the news/depth-
chart data is real-CURRENT — the news tie-in demonstrates the MECHANISM
(backup-surfacing), not temporal coherence with the 2025 value window.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from sqlalchemy import select

from backend.models.player import Player
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_demo_source import seed_demo_league
from backend.services.trade.value_engine import InSeasonValue, evaluate_league
from backend.services.waiver.faab import FAAB_BUDGET_DEFAULT

logger = logging.getLogger(__name__)

# K/DEF streaming arc (slice 3): K/DST now value + seat, so they enter the pool too.
# (The pool comes from players with in-season weekly rows; the seed unions the
# scored K/DST weekly frame, so a free-agent K/DST appears here on flat season value.)
_SKILL = ("QB", "RB", "WR", "TE", "K", "DEF")
# A pool player needs at least this many in-season games to be a real waiver
# candidate (trims one-week blips + bounds the valuation loop).
MIN_POOL_GAMES = 3
_POOL_TEAM_ID = "waiver-pool"


def waiver_demo_enabled() -> bool:
    """Master switch. FALSE/unset in prod ⇒ every waiver demo surface is inert."""
    return os.environ.get("WAIVER_DEMO_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def waiver_demo_enforce_gates() -> bool:
    """Opt-in: apply the real feature gate + credit charge on the demo league
    (default demo behavior bypasses both). Meaningless unless WAIVER_DEMO_MODE is on."""
    return os.environ.get("WAIVER_DEMO_ENFORCE_GATES", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class WaiverDemoSource:
    state: LeagueState                       # the 12 real demo teams
    pool: list[RosterPlayer]                 # available free agents (not rostered)
    values: dict[str, InSeasonValue]         # roster + pool, one anchor basis
    weekly_usage: pd.DataFrame
    priors: dict[str, float] = field(default_factory=dict)
    waiver_type: str = "faab"
    faab_budget: int = FAAB_BUDGET_DEFAULT
    faab_remaining_by_team: dict[str, int] = field(default_factory=dict)
    # {dst_canonical_id: {"opponent", "tilt"}} for the demo week (slice 5a display).
    dst_matchup: dict[str, dict] = field(default_factory=dict)

    def get_league_state(self) -> LeagueState:
        return self.state


def _demo_faab_remaining(team_id: str, idx: int) -> int:
    """Deterministic per-team remaining FAAB (no randomness — stable for tests)."""
    return max(20, FAAB_BUDGET_DEFAULT - (idx * 17) % 80)


async def seed_demo_waiver(db, *, weekly_usage: Optional[pd.DataFrame] = None) -> WaiverDemoSource:
    """Build the waiver demo from the REAL DB: reuse the trade demo's 12-team state
    + per-week layer, add the available pool, and value everyone together."""
    trade_src = await seed_demo_league(db, weekly_usage=weekly_usage)
    state = trade_src.get_league_state()
    weekly = trade_src.weekly_usage
    rostered = state.all_rostered_player_ids()

    pool = await _build_pool(db, weekly, rostered)

    # Value my rosters + the pool in ONE call (augmented LeagueState) so anchors +
    # forward_ppg are consistent across roster and pool.
    pool_team = TeamState(team_id=_POOL_TEAM_ID, team_name="Free Agents", is_me=False, roster=tuple(pool))
    aug = LeagueState(season=state.season, week=state.week, teams=state.teams + (pool_team,))

    from backend.services.trade.trade_demo_source import _load_priors  # reuse the prior loader
    pool_priors_raw = await _load_priors(db, [rp.canonical_player_id for rp in pool])
    priors = {**trade_src.priors, **{k: v for k, v in pool_priors_raw.items() if v is not None}}

    values = evaluate_league(aug, weekly, priors=priors)
    # K/DEF streaming arc (slice 4): DST-only matchup-weekly tilt (offense + kicker
    # untouched). Pool + rostered DST re-rank by the demo week's opponent matchup.
    from backend.services.kdef_matchup import apply_dst_matchup
    from backend.services.trade.trade_demo_source import DEMO_CURRENT_WEEK, DEMO_SEASON
    values, dst_matchup = apply_dst_matchup(values, aug, season=DEMO_SEASON, week=DEMO_CURRENT_WEEK)

    faab_remaining = {
        t.team_id: _demo_faab_remaining(t.team_id, i) for i, t in enumerate(state.teams)
    }
    logger.info(
        "waiver demo seeded: %d rostered, %d available-pool players (>=%d games)",
        len(rostered), len(pool), MIN_POOL_GAMES,
    )
    return WaiverDemoSource(
        state=state, pool=pool, values=values, weekly_usage=weekly, priors=priors,
        faab_remaining_by_team=faab_remaining, dst_matchup=dst_matchup,
    )


async def _build_pool(db, weekly: pd.DataFrame, rostered: set[str]) -> list[RosterPlayer]:
    """Available pool = skill players with >= MIN_POOL_GAMES of in-season data who
    are NOT on any roster. Value comes from the same weekly layer, so anyone in the
    pool is guaranteed to be valuable (no draftable_filter — a breakout is by
    definition not preseason-valued, and hiding breakouts would defeat the point)."""
    if weekly is None or weekly.empty:
        return []
    counts = weekly.groupby("canonical_player_id").size()
    eligible_ids = {str(pid) for pid, n in counts.items() if pid and n >= MIN_POOL_GAMES}
    candidate_ids = eligible_ids - rostered
    if not candidate_ids:
        return []

    uids: list[uuid.UUID] = []
    for x in candidate_ids:
        try:
            uids.append(uuid.UUID(x))
        except (ValueError, TypeError):
            continue

    rows = (await db.execute(
        select(Player.id, Player.name, Player.position, Player.team_abbr)
        .where(Player.id.in_(uids), Player.position.in_(_SKILL))
    )).all()
    return [
        RosterPlayer(canonical_player_id=str(pid), name=name, position=pos, nfl_team=team)
        for pid, name, pos, team in rows
    ]


async def maybe_demo_waiver_source(db) -> Optional[WaiverDemoSource]:
    """THE GATE. Returns the demo source ONLY when WAIVER_DEMO_MODE is on."""
    if not waiver_demo_enabled():
        return None
    return await seed_demo_waiver(db)
