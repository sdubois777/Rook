"""
TEST-ONLY demo league source for the trade feature — FLAG-GATED scaffolding.

⚠️  TEARDOWN (slice 6): this entire module is throwaway. It exists only to build
and validate the trade analyzer/proposals agents against REAL 2025 ragged data
before live in-season data lands. Everything here is gated behind
``TRADE_DEMO_MODE`` (env, default FALSE). When false, ``maybe_demo_league_source``
returns None and nothing in this file is reached on the prod path.

It plants REAL 2025 players (resolved by name → canonical UUID) onto demo teams
and serves the existing ``LeagueState`` seam from the unchanged #149 per-week
data layer. It NEVER fabricates weekly stats — only roster construction and the
demo "current week" anchor (pinned HERE, not in the engine/data layer).

Teardown deletes: this file, ``scripts/seed_demo_league.py``, the
``TRADE_DEMO_MODE`` branches, and the trade-demo tests. Grep ``TRADE_DEMO`` /
``trade_demo`` / ``DEMO_ROSTERS`` to find every surface.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from backend.services.trade.league_state import (
    LeagueState,
    RosterPlayer,
    TeamState,
)

logger = logging.getLogger(__name__)

# --- demo anchor (test-only; pinned here, NOT in the engine/data layer) ------
DEMO_SEASON = 2025
DEMO_CURRENT_WEEK = 14   # late-season trade-window read; gives studs a full trend

# --- the planted cast (data-driven from the real 2025 per-week layer) --------
# Each tuple is (player_name, position); names resolve to canonical UUIDs at seed
# time so the roster is portable across environments. Players were chosen because
# their GENUINE 2025 weekly data exercises a specific ragged-history tier:
#   full studs · sparse(insufficient) · partial(limited) · team-change(limited)
#   · rising(buy_low) · falling(sell_high) · a real rookie.
DEMO_ROSTERS: list[dict] = [
    {
        "team_id": "demo-you", "team_name": "You", "is_me": True,
        "players": [
            ("Christian McCaffrey", "RB"),  # clean stud → full
            ("Puka Nacua", "WR"),           # clean stud → full
            ("A.J. Brown", "WR"),           # rising usage → buy_low (full)
            ("Najee Harris", "RB"),         # 3 played weeks → limited; null prior
            ("DJ Turner", "WR"),            # 1 played week → insufficient
        ],
    },
    {
        "team_id": "demo-rivals", "team_name": "Rivals", "is_me": False,
        "players": [
            ("Jahmyr Gibbs", "RB"),         # clean stud → full
            ("Jonathan Taylor", "RB"),      # clean stud → full
            ("Hunter Renfrow", "WR"),       # declining usage → sell_high (full)
            ("Brandin Cooks", "WR"),        # mid-season team change → limited+flag
            ("Darius Cooper", "WR"),        # real 2025 rookie
        ],
    },
    {
        "team_id": "demo-pretenders", "team_name": "Pretenders", "is_me": False,
        "players": [
            ("Ja'Marr Chase", "WR"),
            ("Bijan Robinson", "RB"),
            ("Trey McBride", "TE"),
            ("George Pickens", "WR"),
            ("Jayden Reed", "WR"),          # 3 played weeks, declining → limited
        ],
    },
]


def trade_demo_enabled() -> bool:
    """Master switch. FALSE/unset in prod ⇒ every demo surface is inert."""
    return os.environ.get("TRADE_DEMO_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class TradeDemoSource:
    """A ``LeagueStateProvider`` (same Protocol as ``StaticLeagueStateProvider``)
    plus the demo's per-week usage window and preseason priors — the extras a
    real in-season provider will later source from live data."""
    state: LeagueState
    weekly_usage: pd.DataFrame
    priors: dict[str, float] = field(default_factory=dict)

    def get_league_state(self) -> LeagueState:
        return self.state


# ---------------------------------------------------------------------------
# pure assembly (DB-free, unit-testable)
# ---------------------------------------------------------------------------
def build_league_state(name_to_player: dict[str, tuple[str, Optional[float]]]) -> LeagueState:
    """Build the demo ``LeagueState`` from resolved players. ``name_to_player``
    maps name → (canonical_player_id, prior_ppg). Unresolved names are skipped
    (logged) so a missing player never crashes the harness."""
    teams: list[TeamState] = []
    for spec in DEMO_ROSTERS:
        roster: list[RosterPlayer] = []
        for name, pos in spec["players"]:
            resolved = name_to_player.get(name)
            if resolved is None:
                logger.warning("demo: could not resolve player %r — skipping", name)
                continue
            roster.append(RosterPlayer(
                canonical_player_id=resolved[0], name=name, position=pos,
            ))
        teams.append(TeamState(
            team_id=spec["team_id"], team_name=spec["team_name"],
            is_me=spec["is_me"], roster=tuple(roster),
        ))
    return LeagueState(season=DEMO_SEASON, week=DEMO_CURRENT_WEEK, teams=tuple(teams))


def build_priors(name_to_player: dict[str, tuple[str, Optional[float]]]) -> dict[str, float]:
    """{canonical_player_id: prior_ppg} for players that have a preseason prior.
    Players with no prior (e.g. a rookie with no clean-season baseline) are
    simply absent → the engine treats them as null-prior (prior_weight 0)."""
    return {
        pid: prior for (pid, prior) in name_to_player.values()
        if prior is not None
    }


# ---------------------------------------------------------------------------
# real seeding (DB + the #149 layer) — guarded; skipped where data is absent
# ---------------------------------------------------------------------------
async def _resolve_players(db) -> dict[str, tuple[str, Optional[float]]]:
    """Resolve every DEMO_ROSTERS name → (canonical UUID, prior_ppg) from the DB.
    Prior = PlayerProfile.clean_season_baseline['ppr_points'] / 17 (preseason
    PPR/game); absent for rookies/unprofiled players → None."""
    from sqlalchemy import select

    from backend.models.player import Player, PlayerProfile

    names = [name for spec in DEMO_ROSTERS for name, _ in spec["players"]]
    rows = (await db.execute(
        select(Player.id, Player.name).where(Player.name.in_(names))
    )).all()
    by_name = {name: str(pid) for pid, name in rows}

    ids = list(by_name.values())
    prior_by_id: dict[str, float] = {}
    if ids:
        prof_rows = (await db.execute(
            select(PlayerProfile.player_id, PlayerProfile.clean_season_baseline)
            .where(PlayerProfile.player_id.in_(ids))
        )).all()
        for pid, baseline in prof_rows:
            if isinstance(baseline, dict) and baseline.get("ppr_points"):
                prior_by_id[str(pid)] = float(baseline["ppr_points"]) / 17.0

    return {
        name: (pid, prior_by_id.get(pid)) for name, pid in by_name.items()
    }


async def seed_demo_league(db, *, weekly_usage: Optional[pd.DataFrame] = None) -> TradeDemoSource:
    """Build the demo source from the REAL DB + per-week layer. ``weekly_usage``
    can be injected (tests) to avoid the live fetch."""
    name_to_player = await _resolve_players(db)
    state = build_league_state(name_to_player)
    priors = build_priors(name_to_player)
    if weekly_usage is None:
        from backend.integrations.nfl_weekly import weekly_player_usage
        weekly_usage = await weekly_player_usage(
            DEMO_SEASON, weeks=range(1, DEMO_CURRENT_WEEK + 1), db=db,
        )
    return TradeDemoSource(state=state, weekly_usage=weekly_usage, priors=priors)


async def maybe_demo_league_source(db) -> Optional[TradeDemoSource]:
    """THE GATE. Returns the demo source ONLY when TRADE_DEMO_MODE is on;
    otherwise None (the prod path selects the real provider, untouched here)."""
    if not trade_demo_enabled():
        return None
    return await seed_demo_league(db)
