"""
TEST-ONLY demo league source for the trade feature — FLAG-GATED scaffolding.

⚠️  TEARDOWN (slice 6): this entire module is throwaway. It exists only to build
and validate the trade analyzer/proposals agents against REAL 2025 ragged data
before live in-season data lands. Everything here is gated behind
``TRADE_DEMO_MODE`` (env, default FALSE). When false, ``maybe_demo_league_source``
returns None and nothing in this file is reached on the prod path.

It builds the demo's 12-team league from a REAL auction draft (``DEMO_ROSTERS`` —
real manager names + the players each actually drafted, K/DST stripped since they
have no usage/snap data and no value-engine anchors). Auction dollar amounts are
NOT modeled: they only ever decided who rostered whom, and roster membership is all
we encode. Player VALUE still comes entirely from the unchanged #149 per-week usage
layer; starter slots are re-derived per team from that forward value (NOT draft
cost). It NEVER fabricates weekly stats. Only roster membership + the demo "current
week" anchor live here.

Teardown deletes: this file, ``scripts/seed_demo_league.py``, the
``TRADE_DEMO_MODE`` branches, and the trade-demo tests. Grep ``TRADE_DEMO`` /
``trade_demo`` / ``DEMO_ROSTERS`` / ``DEMO_TEAM_NAMES`` to find every surface.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from backend.services.trade.league_state import (
    LeagueState,
    RosterPlayer,
    TeamState,
)
from backend.services.trade.value_engine import evaluate_league

logger = logging.getLogger(__name__)

# --- demo anchor (test-only; pinned here, NOT in the engine/data layer) ------
DEMO_SEASON = 2025
DEMO_CURRENT_WEEK = 14   # late-season trade-window read; gives studs a full trend

# --- league shape ------------------------------------------------------------
N_TEAMS = 12
_SKILL = ("QB", "RB", "WR", "TE")   # K/DST excluded — no usage/snap data, no anchors
# Starting lineup: 1 QB, 2 RB, 3 WR, 1 TE, 1 FLEX (RB/WR/TE) = 8 starters.
STARTER_NEED = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
N_STARTERS = sum(STARTER_NEED.values()) + 1   # + FLEX
_FLEX_POS = ("RB", "WR", "TE")
N_FLEX = 1

# The user's own team (the default acting-as team). is_me is keyed off this name.
USER_TEAM_NAME = "Have you seen McConkeys"

# --- the REAL auction draft (K/DST stripped) ---------------------------------
# (manager_name, ((player_name, position), ...)). Roster membership only — auction
# prices aren't modeled (they merely decided who drafted whom). Team order is the
# draft's; is_me is whichever team == USER_TEAM_NAME. 159 skill players total.
DEMO_ROSTERS: list[tuple[str, tuple[tuple[str, str], ...]]] = [
    ("The Lord", (
        ("Drake London", "WR"), ("Jahmyr Gibbs", "RB"), ("CeeDee Lamb", "WR"),
        ("D'Andre Swift", "RB"), ("Caleb Williams", "QB"),
        ("Jacory Croskey-Merritt", "RB"), ("Javonte Williams", "RB"),
        ("Kaleb Johnson", "RB"), ("Kyler Murray", "QB"), ("Tyler Warren", "TE"),
        ("Joe Mixon", "RB"), ("Brian Robinson", "RB"), ("Isaac TeSlaa", "WR"),
    )),
    ("GOAT C.", (
        ("Amon-Ra St. Brown", "WR"), ("Saquon Barkley", "RB"), ("Tee Higgins", "WR"),
        ("James Cook III", "RB"), ("Patrick Mahomes", "QB"), ("Jordan Mason", "RB"),
        ("Dallas Goedert", "TE"), ("Rashod Bateman", "WR"), ("Darnell Mooney", "WR"),
        ("Bryce Young", "QB"), ("Trey Benson", "RB"), ("Chig Okonkwo", "TE"),
        ("Brandon Aiyuk", "WR"),
    )),
    ("Fat Bastard", (
        ("A.J. Brown", "WR"), ("Tetairoa McMillan", "WR"), ("Mike Evans", "WR"),
        ("DeVonta Smith", "WR"), ("Josh Jacobs", "RB"), ("Joe Burrow", "QB"),
        ("Kenneth Walker III", "RB"), ("Mark Andrews", "TE"),
        ("Chris Godwin Jr.", "WR"), ("Jayden Reed", "WR"), ("Dylan Sampson", "RB"),
        ("Hunter Henry", "TE"), ("Cam Ward", "QB"),
    )),
    ("Break your leg CMC", (
        ("Ja'Marr Chase", "WR"), ("Malik Nabers", "WR"), ("Jaxon Smith-Njigba", "WR"),
        ("Breece Hall", "RB"), ("Tyrone Tracy Jr.", "RB"), ("Justice Hill", "RB"),
        ("Keon Coleman", "WR"), ("Jared Goff", "QB"), ("Isaiah Likely", "TE"),
        ("Will Shipley", "RB"), ("Darren Waller", "TE"), ("Kenny Gainwell", "RB"),
        ("Ray Davis", "RB"), ("Sam Darnold", "QB"),
    )),
    (USER_TEAM_NAME, (
        ("Lamar Jackson", "QB"), ("Brock Bowers", "TE"), ("Ladd McConkey", "WR"),
        ("Courtland Sutton", "WR"), ("Davante Adams", "WR"), ("James Conner", "RB"),
        ("David Montgomery", "RB"), ("DJ Moore", "WR"), ("Jordan Addison", "WR"),
        ("Tyler Allgeier", "RB"), ("Kareem Hunt", "RB"), ("Najee Harris", "RB"),
        ("Raheem Mostert", "RB"),
    )),
    ("Joe Shiesty", (
        ("De'Von Achane", "RB"), ("Bijan Robinson", "RB"), ("Terry McLaurin", "WR"),
        ("George Pickens", "WR"), ("Brock Purdy", "QB"), ("Zay Flowers", "WR"),
        ("T.J. Hockenson", "TE"), ("Travis Hunter", "WR"), ("Jaylen Warren", "RB"),
        ("Bhayshul Tuten", "RB"), ("Wan'Dale Robinson", "WR"), ("C.J. Stroud", "QB"),
        ("Jaylen Waddle", "WR"),
    )),
    ("PAIN", (
        ("Ashton Jeanty", "RB"), ("Trey McBride", "TE"), ("Jalen Hurts", "QB"),
        ("Tyreek Hill", "WR"), ("DK Metcalf", "WR"), ("TreVeyon Henderson", "RB"),
        ("Khalil Shakir", "WR"), ("J.K. Dobbins", "RB"), ("Matthew Golden", "WR"),
        ("Nick Chubb", "RB"), ("Josh Downs", "WR"), ("Jerome Ford", "RB"),
        ("Rhamondre Stevenson", "RB"), ("Christian Kirk", "WR"),
    )),
    ("Big Black Cop", (
        ("Bucky Irving", "RB"), ("Chase Brown", "RB"), ("Brian Thomas Jr.", "WR"),
        ("Calvin Ridley", "WR"), ("Ricky Pearsall", "WR"), ("Rome Odunze", "WR"),
        ("Tucker Kraft", "TE"), ("Jakobi Meyers", "WR"), ("Braelon Allen", "RB"),
        ("Keenan Allen", "WR"), ("Colston Loveland", "TE"), ("Rashid Shaheed", "WR"),
        ("Drake Maye", "QB"), ("Rachaad White", "RB"),
    )),
    ("Watson's Rub and...", (
        ("Christian McCaffrey", "RB"), ("Puka Nacua", "WR"), ("Jonathan Taylor", "RB"),
        ("Tony Pollard", "RB"), ("Bo Nix", "QB"), ("Emeka Egbuka", "WR"),
        ("Rashee Rice", "WR"), ("Jerry Jeudy", "WR"), ("Zach Charbonnet", "RB"),
        ("Cam Skattebo", "RB"), ("Dak Prescott", "QB"), ("Evan Engram", "TE"),
        ("Kyle Pitts Sr.", "TE"),
    )),
    ("Koo Klux Klan", (
        ("Jameson Williams", "WR"), ("Nico Collins", "WR"), ("Kyren Williams", "RB"),
        ("Xavier Worthy", "WR"), ("Chuba Hubbard", "RB"), ("Marvin Harrison Jr.", "WR"),
        ("Baker Mayfield", "QB"), ("Sam LaPorta", "TE"), ("Stefon Diggs", "WR"),
        ("J.J. McCarthy", "QB"), ("Travis Etienne Jr.", "RB"), ("Dalton Kincaid", "TE"),
        ("Tyjae Spears", "RB"),
    )),
    ("Agent Orange", (
        ("Justin Jefferson", "WR"), ("Derrick Henry", "RB"), ("Jayden Daniels", "QB"),
        ("RJ Harvey", "RB"), ("Isiah Pacheco", "RB"), ("Austin Ekeler", "RB"),
        ("Michael Pittman Jr.", "WR"), ("David Njoku", "TE"), ("Jordan Love", "QB"),
        ("Jauan Jennings", "WR"), ("Tank Bigsby", "RB"), ("Jake Ferguson", "TE"),
        ("Justin Herbert", "QB"),
    )),
    ("Ben Dover", (
        ("Josh Allen", "QB"), ("Alvin Kamara", "RB"), ("George Kittle", "TE"),
        ("Omarion Hampton", "RB"), ("Garrett Wilson", "WR"), ("Chris Olave", "WR"),
        ("Deebo Samuel", "WR"), ("Travis Kelce", "TE"), ("Aaron Jones Sr.", "RB"),
        ("Cooper Kupp", "WR"), ("Justin Fields", "QB"), ("Jaxson Dart", "QB"),
        ("Trevor Lawrence", "QB"),
    )),
]

# Display alias for greppability / parity with the old constant name.
DEMO_TEAM_NAMES = [mgr for mgr, _ in DEMO_ROSTERS]


def trade_demo_enabled() -> bool:
    """Master switch. FALSE/unset in prod ⇒ every demo surface is inert."""
    return os.environ.get("TRADE_DEMO_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def trade_demo_enforce_gates() -> bool:
    """Opt-in: apply the real feature gate + credit charge even while running on
    the demo league (default demo behavior is to bypass both). Lets us TEST the
    tier gate and credit deduction against the seeded demo data + sandbox Stripe.
    Off ⇒ demo keeps its bypass. Meaningless unless TRADE_DEMO_MODE is on."""
    return os.environ.get("TRADE_DEMO_ENFORCE_GATES", "").strip().lower() in {
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
def assemble_teams(
    rosters: list[tuple[str, tuple[tuple[str, str], ...]]],
    resolve: Callable[[str, str], Optional[tuple[str, Optional[str]]]],
) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Turn ``DEMO_ROSTERS`` into ``teams_data`` via an injected resolver.

    ``resolve(name, position)`` returns ``(canonical_player_id, nfl_team)`` or
    ``None`` when the player can't be resolved. Unresolved players are NEVER
    silently dropped — they're collected and returned as ``(manager, name,
    position)`` so the caller can report them. Pure (resolver injected) → testable
    with no DB. ``starter_slot`` is left BENCH here; it's re-derived from forward
    value after the engine runs.
    """
    teams_data: list[dict] = []
    unresolved: list[tuple[str, str, str]] = []
    for idx, (manager, picks) in enumerate(rosters):
        players: list[dict] = []
        for name, pos in picks:
            resolved = resolve(name, pos)
            if resolved is None:
                unresolved.append((manager, name, pos))
                continue
            pid, nfl_team = resolved
            players.append({
                "id": pid, "name": name, "position": pos,
                "nfl_team": nfl_team, "starter_slot": "BENCH",
            })
        teams_data.append({
            "team_id": f"demo-team-{idx}", "team_name": manager,
            "is_me": manager == USER_TEAM_NAME, "players": players,
        })
    return teams_data, unresolved


def assign_starter_slots(players: list[dict], value_by_id: dict[str, float]) -> None:
    """Assign the starting lineup in place by FORWARD VALUE (not draft cost).
    ``players`` is one team's roster as dicts (each with 'id' + 'position');
    the highest-value players fill 1QB/2RB/3WR/1TE/1FLEX, the rest are BENCH.
    Mutates each dict's 'starter_slot'."""
    ordered = sorted(players, key=lambda p: (-value_by_id.get(p["id"], 0.0), p["name"]))
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
async def _resolve_rosters(db) -> tuple[list[dict], dict[str, Optional[float]]]:
    """Resolve every drafted player to a Rook canonical id (== the #149 layer's
    ``canonical_player_id``), preserving nfl_team + the drafted position. Resolution
    is name-fuzzy (``find_by_name_fuzzy`` — handles Jr/Sr/II + initials, the same
    crosswalk-backed path the live draft uses). Unresolved players are reported,
    never silently dropped. Returns (teams_data, prior_by_id)."""
    from backend.repositories.player_repo import PlayerRepository

    repo = PlayerRepository(db)
    # Resolve each distinct drafted name once.
    resolved: dict[str, Optional[tuple[str, Optional[str]]]] = {}
    for _, picks in DEMO_ROSTERS:
        for name, _pos in picks:
            if name in resolved:
                continue
            player = await repo.find_by_name_fuzzy(name)
            resolved[name] = (str(player.id), player.team_abbr) if player else None

    teams_data, unresolved = assemble_teams(DEMO_ROSTERS, lambda n, pos: resolved.get(n))

    if unresolved:
        total = sum(len(picks) for _, picks in DEMO_ROSTERS)
        for manager, name, pos in unresolved:
            logger.warning(
                "demo seed: unresolved draft player %r (%s) on %r — dropped",
                name, pos, manager,
            )
        logger.warning(
            "demo seed: %d/%d drafted players unresolved (no DB match)",
            len(unresolved), total,
        )

    prior_by_id = await _load_priors(db, [p["id"] for t in teams_data for p in t["players"]])
    return teams_data, prior_by_id


async def _load_priors(db, rostered_ids: list[str]) -> dict[str, Optional[float]]:
    """Preseason prior ppg per rostered player (clean_season_baseline ppr / 17).
    Players with no profile/baseline get None (null prior → prior_weight 0)."""
    from sqlalchemy import select

    from backend.models.player import PlayerProfile

    prior_by_id: dict[str, Optional[float]] = {pid: None for pid in rostered_ids}
    if not rostered_ids:
        return prior_by_id
    rows = (await db.execute(
        select(PlayerProfile.player_id, PlayerProfile.clean_season_baseline)
        .where(PlayerProfile.player_id.in_(rostered_ids))
    )).all()
    for pid, baseline in rows:
        if isinstance(baseline, dict) and baseline.get("ppr_points"):
            prior_by_id[str(pid)] = float(baseline["ppr_points"]) / 17.0
    return prior_by_id


async def seed_demo_league(db, *, weekly_usage: Optional[pd.DataFrame] = None) -> TradeDemoSource:
    """Build the demo source from the REAL DB + per-week layer. ``weekly_usage``
    can be injected (tests) to avoid the live fetch. Starter slots are re-derived
    from the engine's forward value, so the lineup reflects in-season production —
    not draft order."""
    teams_data, prior_by_id = await _resolve_rosters(db)
    priors = build_priors(prior_by_id)
    if weekly_usage is None:
        from backend.integrations.nfl_weekly import weekly_player_usage
        weekly_usage = await weekly_player_usage(
            DEMO_SEASON, weeks=range(1, DEMO_CURRENT_WEEK + 1), db=db,
        )

    # Forward value first (slot-agnostic), then slot the lineup by that value.
    provisional = build_league_state(teams_data)
    values = evaluate_league(provisional, weekly_usage, priors=priors)
    value_by_id = {pid: v.forward_value for pid, v in values.items()}
    for team in teams_data:
        assign_starter_slots(team["players"], value_by_id)

    state = build_league_state(teams_data)
    return TradeDemoSource(state=state, weekly_usage=weekly_usage, priors=priors)


async def maybe_demo_league_source(db) -> Optional[TradeDemoSource]:
    """THE GATE. Returns the demo source ONLY when TRADE_DEMO_MODE is on;
    otherwise None (the prod path selects the real provider, untouched here)."""
    if not trade_demo_enabled():
        return None
    return await seed_demo_league(db)
