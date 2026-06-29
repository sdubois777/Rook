"""
TEST-ONLY demo league source for the trade feature — FLAG-GATED scaffolding.

⚠️  TEARDOWN (slice 6): this entire module is throwaway. It exists only to build
and validate the trade analyzer/proposals agents against REAL 2025 ragged data
before live in-season data lands. Everything here is gated behind
``TRADE_DEMO_MODE`` (env, default FALSE). When false, ``maybe_demo_league_source``
returns None and nothing in this file is reached on the prod path.

It builds a realistic 12-team league by snake-drafting REAL 2025 skill players
from the ADP pool (deterministic) and force-placing the CASTING players so every
confidence tier + the team-change case stay present. It NEVER fabricates weekly
stats — value comes from the unchanged #149 per-week layer. Only roster
construction + the demo "current week" anchor live here.

Teardown deletes: this file, ``scripts/seed_demo_league.py``, the
``TRADE_DEMO_MODE`` branches, and the trade-demo tests. Grep ``TRADE_DEMO`` /
``trade_demo`` / ``DEMO_ROSTERS`` / ``CASTING`` / ``DEMO_TEAM_NAMES`` to find
every surface.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
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

# --- league shape ------------------------------------------------------------
N_TEAMS = 12
TEAM_SIZE = 15
_SKILL = ("QB", "RB", "WR", "TE")   # K/DST excluded — no usage/snap data
# Per-team roster quotas (sum == TEAM_SIZE) so every team can field a lineup.
DRAFT_QUOTAS = {"QB": 2, "RB": 5, "WR": 6, "TE": 2}
# Starting lineup: 1 QB, 2 RB, 3 WR, 1 TE, 1 FLEX (RB/WR/TE) = 8 starters, 7 bench.
STARTER_NEED = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
_FLEX_POS = ("RB", "WR", "TE")
N_FLEX = 1

# 12 believable team names; index 0 is the is_me team.
DEMO_TEAM_NAMES = [
    "Your Squad",            # is_me
    "Gridiron Gurus",
    "End Zone Elites",
    "Hail Mary Heroes",
    "Blitz Brigade",
    "Pigskin Prophets",
    "Fourth & Long",
    "Audible Anarchy",
    "Red Zone Raiders",
    "Checkdown Kings",
    "Gronk & Roll",
    "Victory Formation",
]

# --- the planted cast (data-driven; preserves every ragged-history tier) ------
# (player_name, position, team_index) — force-placed so the tiers + team-change
# case survive the larger league. Spread ≤2 per team for a balanced distribution.
#   studs(full) · sparse(insufficient) · partial(limited) · team-change(limited)
#   · rising(buy_low) · falling(sell_high) · a real rookie.
CASTING: list[tuple[str, str, int]] = [
    ("Christian McCaffrey", "RB", 0),  # stud → full
    ("A.J. Brown", "WR", 0),           # rising → buy_low
    ("Jahmyr Gibbs", "RB", 1),         # stud → full
    ("DJ Turner", "WR", 1),            # 1 played week → insufficient
    ("Jonathan Taylor", "RB", 2),      # stud → full
    ("Hunter Renfrow", "WR", 2),       # declining → sell_high
    ("Brandin Cooks", "WR", 3),        # mid-season team change → limited+flag
    ("Darius Cooper", "WR", 3),        # real 2025 rookie
    ("Ja'Marr Chase", "WR", 4),        # stud
    ("Najee Harris", "RB", 4),         # 3 played weeks → limited
    ("Bijan Robinson", "RB", 5),       # stud
    ("Jayden Reed", "WR", 5),          # 3 played weeks, declining → limited
    ("Trey McBride", "TE", 6),         # stud TE
    ("Puka Nacua", "WR", 6),           # stud
    ("George Pickens", "WR", 7),       # stud
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
def assign_starter_slots(players: list[dict]) -> None:
    """Assign a sensible starting lineup in place. ``players`` is one team's
    roster as dicts (each with 'position' + 'adp'); best-by-ADP fill the lineup,
    the rest are BENCH. Mutates each dict's 'starter_slot'."""
    ordered = sorted(players, key=lambda p: p.get("adp", 9999.0))
    need = dict(STARTER_NEED)
    counts: Counter = Counter()
    flex_left = N_FLEX
    for p in ordered:
        pos = p["position"]
        if need.get(pos, 0) > 0:
            need[pos] -= 1
            counts[pos] += 1
            p["starter_slot"] = f"{pos}{counts[pos]}" if STARTER_NEED.get(pos, 0) > 1 else pos
        elif flex_left > 0 and pos in _FLEX_POS:
            flex_left -= 1
            p["starter_slot"] = "FLEX"
        else:
            p["starter_slot"] = "BENCH"


def build_league_state(teams_data: list[dict]) -> LeagueState:
    """Build the demo ``LeagueState`` from generated team data. ``teams_data`` is
    a list of {team_id, team_name, is_me, players:[{id,name,position,nfl_team,
    starter_slot}]}. Pure — no DB."""
    teams = tuple(
        TeamState(
            team_id=t["team_id"], team_name=t["team_name"], is_me=t["is_me"],
            roster=tuple(
                RosterPlayer(
                    canonical_player_id=p["id"], name=p["name"], position=p["position"],
                    nfl_team=p.get("nfl_team"), starter_slot=p.get("starter_slot"),
                )
                for p in t["players"]
            ),
        )
        for t in teams_data
    )
    return LeagueState(season=DEMO_SEASON, week=DEMO_CURRENT_WEEK, teams=teams)


def build_priors(prior_by_id: dict[str, Optional[float]]) -> dict[str, float]:
    """{canonical_player_id: prior_ppg} for players that have a preseason prior.
    Players with no prior (rookie / unprofiled veteran) are absent → the engine
    treats them as null-prior (prior_weight 0)."""
    return {pid: pr for pid, pr in prior_by_id.items() if pr is not None}


# ---------------------------------------------------------------------------
# real generation (DB + the #149 layer) — guarded; skipped where data is absent
# ---------------------------------------------------------------------------
async def _draft_league(db) -> tuple[list[dict], dict[str, Optional[float]]]:
    """Deterministically build a 12-team league: snake-draft the real ADP pool,
    force-place CASTING, populate nfl_team + starter_slot. Returns
    (teams_data, prior_by_id)."""
    from sqlalchemy import select

    from backend.models.player import Player, PlayerProfile

    # 1. The ADP pool — skill players, best first.
    rows = (await db.execute(
        select(Player.id, Player.name, Player.team_abbr, Player.position, Player.adp_ai)
        .where(Player.adp_ai.isnot(None), Player.position.in_(_SKILL))
        .order_by(Player.adp_ai.asc())
    )).all()
    pool: list[dict] = [
        {"id": str(i), "name": n, "nfl_team": ta, "position": pos, "adp": float(a)}
        for i, n, ta, pos, a in rows
    ]

    # 2. Resolve CASTING by name (some sit outside the ADP pool, e.g. a rookie).
    cast_names = [c[0] for c in CASTING]
    crows = (await db.execute(
        select(Player.id, Player.name, Player.team_abbr, Player.position, Player.adp_ai)
        .where(Player.name.in_(cast_names))
    )).all()
    cast_db = {n: (str(i), ta, a) for i, n, ta, pos, a in crows}

    teams: list[dict] = [
        {"team_id": f"demo-team-{idx}", "team_name": name, "is_me": idx == 0, "players": []}
        for idx, name in enumerate(DEMO_TEAM_NAMES)
    ]

    cast_ids: set[str] = set()
    for name, pos, ti in CASTING:
        rec = cast_db.get(name)
        if rec is None:
            logger.warning("demo: casting player %r not in DB — skipping", name)
            continue
        pid, ta, adp = rec
        cast_ids.add(pid)
        teams[ti]["players"].append({
            "id": pid, "name": name, "nfl_team": ta, "position": pos,
            "adp": float(adp) if adp is not None else 9999.0,
        })

    # 3. Snake-draft the rest, respecting per-position quotas (lineup-viable).
    by_pos: dict[str, list[dict]] = {
        pos: [p for p in pool if p["position"] == pos and p["id"] not in cast_ids]
        for pos in _SKILL
    }

    def _pick_for(team: dict) -> Optional[dict]:
        cnt = Counter(p["position"] for p in team["players"])
        needed = [pos for pos in _SKILL
                  if cnt.get(pos, 0) < DRAFT_QUOTAS[pos] and by_pos[pos]]
        candidates = needed or [pos for pos in _SKILL if by_pos[pos]]
        if not candidates:
            return None
        best_pos = min(candidates, key=lambda pos: by_pos[pos][0]["adp"])
        return by_pos[best_pos].pop(0)

    order = list(range(N_TEAMS))
    for round_i in range(TEAM_SIZE + 5):  # safety bound
        if all(len(t["players"]) >= TEAM_SIZE for t in teams):
            break
        seq = order if round_i % 2 == 0 else order[::-1]
        for ti in seq:
            team = teams[ti]
            if len(team["players"]) >= TEAM_SIZE:
                continue
            pick = _pick_for(team)
            if pick is not None:
                team["players"].append(pick)

    # 4. Starter slots per team (best-by-ADP start; rest bench).
    for team in teams:
        assign_starter_slots(team["players"])

    # 5. Priors for every rostered player (clean_season_baseline ppr / 17).
    rostered_ids = [p["id"] for t in teams for p in t["players"]]
    prior_by_id: dict[str, Optional[float]] = {pid: None for pid in rostered_ids}
    if rostered_ids:
        prof_rows = (await db.execute(
            select(PlayerProfile.player_id, PlayerProfile.clean_season_baseline)
            .where(PlayerProfile.player_id.in_(rostered_ids))
        )).all()
        for pid, baseline in prof_rows:
            if isinstance(baseline, dict) and baseline.get("ppr_points"):
                prior_by_id[str(pid)] = float(baseline["ppr_points"]) / 17.0

    return teams, prior_by_id


async def seed_demo_league(db, *, weekly_usage: Optional[pd.DataFrame] = None) -> TradeDemoSource:
    """Build the demo source from the REAL DB + per-week layer. ``weekly_usage``
    can be injected (tests) to avoid the live fetch."""
    teams_data, prior_by_id = await _draft_league(db)
    state = build_league_state(teams_data)
    priors = build_priors(prior_by_id)
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
