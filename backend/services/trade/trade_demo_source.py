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

import asyncio
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
# EXPLICIT demo-week OVERRIDE — NOT the canonical week source. A real league's week
# derives from backend.utils.seasons.get_current_nfl_week (over the cached schedule);
# the demo deliberately PINS an arbitrary week so we can seed any point in the season
# (currently a late-season read that gives studs a full trend, and lets us later seed
# an early week to exercise the low-sample blend). Keep this pin — it is how the demo
# sets its own week — but it is no longer the SOLE week source.
DEMO_SEASON = 2025
DEMO_CURRENT_WEEK = 14   # late-season trade-window read; gives studs a full trend

# --- league shape ------------------------------------------------------------
N_TEAMS = 12
# K/DEF streaming arc (slice 3): K/DST now VALUE (slice 2) + SEAT (lineup slots),
# so they're un-stripped — first-class roster + pool + lineup positions.
_SKILL = ("QB", "RB", "WR", "TE", "K", "DEF")
# The demo league is 2-WR BY DESIGN: 1 QB / 2 RB / 2 WR / 1 TE / 1 FLEX / 1 K / 1 DST.
# roster_slots is the canonical (services/roster_slots) shape carried on the demo
# LeagueState and fed to lineup_rules_from_slots — so the matchup optimal lineup uses
# the REAL bridge, not the hardcoded 3-WR DEFAULT_LINEUP_RULES (which stays shared with
# trade). STARTER_NEED (the roster-BUILD slot assignment) must agree at 2-WR.
DEMO_ROSTER_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1}
STARTER_NEED = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
N_STARTERS = sum(STARTER_NEED.values()) + 1   # + FLEX
_FLEX_POS = ("RB", "WR", "TE")
N_FLEX = 1

# The user's own team (the default acting-as team). is_me is keyed off this name.
USER_TEAM_NAME = "Have you seen McConkeys"

# TEARDOWN (demo-only): a small injury map so the status BADGE is visibly working
# pre-launch. The demo is pinned to 2025 wk14, so live Sleeper status (2026) can't be
# overlaid coherently — these are seeded DEMO values (canonical codes), keyed by the
# player name in DEMO_ROSTERS, all on the user's own team for guaranteed visibility.
# Grep DEMO_INJURIES to remove with the rest of the demo scaffolding.
DEMO_INJURIES: dict[str, str] = {
    "Ladd McConkey": "Q",
    "Davante Adams": "D",
    "Courtland Sutton": "O",
    "James Conner": "IR",
}

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
        ("Harrison Butker", "K"), ("Kansas City Chiefs", "DEF"),
    )),
    ("GOAT C.", (
        ("Amon-Ra St. Brown", "WR"), ("Saquon Barkley", "RB"), ("Tee Higgins", "WR"),
        ("James Cook III", "RB"), ("Patrick Mahomes", "QB"), ("Jordan Mason", "RB"),
        ("Dallas Goedert", "TE"), ("Rashod Bateman", "WR"), ("Darnell Mooney", "WR"),
        ("Bryce Young", "QB"), ("Trey Benson", "RB"), ("Chig Okonkwo", "TE"),
        ("Brandon Aiyuk", "WR"), ("Jake Elliott", "K"), ("Philadelphia Eagles", "DEF"),
    )),
    ("Fat Bastard", (
        ("A.J. Brown", "WR"), ("Tetairoa McMillan", "WR"), ("Mike Evans", "WR"),
        ("DeVonta Smith", "WR"), ("Josh Jacobs", "RB"), ("Joe Burrow", "QB"),
        ("Kenneth Walker III", "RB"), ("Mark Andrews", "TE"),
        ("Chris Godwin Jr.", "WR"), ("Jayden Reed", "WR"), ("Dylan Sampson", "RB"),
        ("Hunter Henry", "TE"), ("Cam Ward", "QB"),
        ("Chris Boswell", "K"), ("Pittsburgh Steelers", "DEF"),
    )),
    ("Break your leg CMC", (
        ("Ja'Marr Chase", "WR"), ("Malik Nabers", "WR"), ("Jaxon Smith-Njigba", "WR"),
        ("Breece Hall", "RB"), ("Tyrone Tracy Jr.", "RB"), ("Justice Hill", "RB"),
        ("Keon Coleman", "WR"), ("Jared Goff", "QB"), ("Isaiah Likely", "TE"),
        ("Will Shipley", "RB"), ("Darren Waller", "TE"), ("Kenny Gainwell", "RB"),
        ("Ray Davis", "RB"), ("Sam Darnold", "QB"),
        ("Jason Myers", "K"), ("Seattle Seahawks", "DEF"),
    )),
    (USER_TEAM_NAME, (
        ("Lamar Jackson", "QB"), ("Brock Bowers", "TE"), ("Ladd McConkey", "WR"),
        ("Courtland Sutton", "WR"), ("Davante Adams", "WR"), ("James Conner", "RB"),
        ("David Montgomery", "RB"), ("DJ Moore", "WR"), ("Jordan Addison", "WR"),
        ("Tyler Allgeier", "RB"), ("Kareem Hunt", "RB"), ("Najee Harris", "RB"),
        ("Raheem Mostert", "RB"), ("Wil Lutz", "K"), ("Denver Broncos", "DEF"),
    )),
    ("Joe Shiesty", (
        ("De'Von Achane", "RB"), ("Bijan Robinson", "RB"), ("Terry McLaurin", "WR"),
        ("George Pickens", "WR"), ("Brock Purdy", "QB"), ("Zay Flowers", "WR"),
        ("T.J. Hockenson", "TE"), ("Travis Hunter", "WR"), ("Jaylen Warren", "RB"),
        ("Bhayshul Tuten", "RB"), ("Wan'Dale Robinson", "WR"), ("C.J. Stroud", "QB"),
        ("Jaylen Waddle", "WR"), ("Jason Sanders", "K"), ("Miami Dolphins", "DEF"),
    )),
    ("PAIN", (
        ("Ashton Jeanty", "RB"), ("Trey McBride", "TE"), ("Jalen Hurts", "QB"),
        ("Tyreek Hill", "WR"), ("DK Metcalf", "WR"), ("TreVeyon Henderson", "RB"),
        ("Khalil Shakir", "WR"), ("J.K. Dobbins", "RB"), ("Matthew Golden", "WR"),
        ("Nick Chubb", "RB"), ("Josh Downs", "WR"), ("Jerome Ford", "RB"),
        ("Rhamondre Stevenson", "RB"), ("Christian Kirk", "WR"),
        ("Matt Gay", "K"), ("Washington Commanders", "DEF"),
    )),
    ("Big Black Cop", (
        ("Bucky Irving", "RB"), ("Chase Brown", "RB"), ("Brian Thomas Jr.", "WR"),
        ("Calvin Ridley", "WR"), ("Ricky Pearsall", "WR"), ("Rome Odunze", "WR"),
        ("Tucker Kraft", "TE"), ("Jakobi Meyers", "WR"), ("Braelon Allen", "RB"),
        ("Keenan Allen", "WR"), ("Colston Loveland", "TE"), ("Rashid Shaheed", "WR"),
        ("Drake Maye", "QB"), ("Rachaad White", "RB"),
        ("Nick Folk", "K"), ("New York Jets", "DEF"),
    )),
    ("Watson's Rub and...", (
        ("Christian McCaffrey", "RB"), ("Puka Nacua", "WR"), ("Jonathan Taylor", "RB"),
        ("Tony Pollard", "RB"), ("Bo Nix", "QB"), ("Emeka Egbuka", "WR"),
        ("Rashee Rice", "WR"), ("Jerry Jeudy", "WR"), ("Zach Charbonnet", "RB"),
        ("Cam Skattebo", "RB"), ("Dak Prescott", "QB"), ("Evan Engram", "TE"),
        ("Kyle Pitts Sr.", "TE"), ("Ka'imi Fairbairn", "K"), ("Houston Texans", "DEF"),
    )),
    ("Koo Klux Klan", (
        ("Jameson Williams", "WR"), ("Nico Collins", "WR"), ("Kyren Williams", "RB"),
        ("Xavier Worthy", "WR"), ("Chuba Hubbard", "RB"), ("Marvin Harrison Jr.", "WR"),
        ("Baker Mayfield", "QB"), ("Sam LaPorta", "TE"), ("Stefon Diggs", "WR"),
        ("J.J. McCarthy", "QB"), ("Travis Etienne Jr.", "RB"), ("Dalton Kincaid", "TE"),
        ("Tyjae Spears", "RB"), ("Cairo Santos", "K"), ("Detroit Lions", "DEF"),
    )),
    ("Agent Orange", (
        ("Justin Jefferson", "WR"), ("Derrick Henry", "RB"), ("Jayden Daniels", "QB"),
        ("RJ Harvey", "RB"), ("Isiah Pacheco", "RB"), ("Austin Ekeler", "RB"),
        ("Michael Pittman Jr.", "WR"), ("David Njoku", "TE"), ("Jordan Love", "QB"),
        ("Jauan Jennings", "WR"), ("Tank Bigsby", "RB"), ("Jake Ferguson", "TE"),
        ("Justin Herbert", "QB"), ("Chase McLaughlin", "K"), ("Minnesota Vikings", "DEF"),
    )),
    ("Ben Dover", (
        ("Josh Allen", "QB"), ("Alvin Kamara", "RB"), ("George Kittle", "TE"),
        ("Omarion Hampton", "RB"), ("Garrett Wilson", "WR"), ("Chris Olave", "WR"),
        ("Deebo Samuel", "WR"), ("Travis Kelce", "TE"), ("Aaron Jones Sr.", "RB"),
        ("Cooper Kupp", "WR"), ("Justin Fields", "QB"), ("Jaxson Dart", "QB"),
        ("Trevor Lawrence", "QB"), ("Eddy Pineiro", "K"), ("Buffalo Bills", "DEF"),
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
    scoring_format: str = "ppr"   # league's format (in-season re-score basis; PPR default)

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
                "injury_status": DEMO_INJURIES.get(name),  # demo badge (None = healthy)
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
                    injury_status=p.get("injury_status"),
                )
                for p in t["players"]
            ),
        )
        for t in teams_data
    )
    return LeagueState(season=DEMO_SEASON, week=DEMO_CURRENT_WEEK, teams=teams,
                       roster_slots=dict(DEMO_ROSTER_SLOTS))


def build_priors(prior_by_id: dict[str, Optional[float]]) -> dict[str, float]:
    """{canonical_player_id: prior_ppg} for players that have a preseason prior.
    Players with no prior (rookie / unprofiled veteran) are absent → the engine
    treats them as null-prior (prior_weight 0)."""
    return {pid: pr for pid, pr in prior_by_id.items() if pr is not None}


# ---------------------------------------------------------------------------
# real generation (DB + the #149 layer) — guarded; skipped where data is absent
# ---------------------------------------------------------------------------
async def _resolve_rosters(
    db, scoring_format: str = "ppr"
) -> tuple[list[dict], dict[str, Optional[float]]]:
    """Resolve every drafted player to a Rook canonical id (== the #149 layer's
    ``canonical_player_id``), preserving nfl_team + the drafted position. Resolution
    is name-fuzzy (``find_by_name_fuzzy`` — handles Jr/Sr/II + initials, the same
    crosswalk-backed path the live draft uses). Unresolved players are reported,
    never silently dropped. Returns (teams_data, prior_by_id)."""
    from backend.repositories.player_repo import PlayerRepository

    repo = PlayerRepository(db)
    # Resolve each distinct drafted (name, position) once via the SAME canonical
    # resolve_player the real provider uses (one resolution path everywhere, #243) —
    # the demo has no platform ids, so this is the guarded-name path WITH the position
    # filter (DEF routes to the team map). Replaces the old bare find_by_name_fuzzy.
    resolved: dict[str, Optional[tuple[str, Optional[str]]]] = {}
    for _, picks in DEMO_ROSTERS:
        for name, pos in picks:
            if name in resolved:
                continue
            player = await repo.resolve_player(name=name, position=pos, team=None)
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

    prior_by_id = await _load_priors(
        db, [p["id"] for t in teams_data for p in t["players"]], scoring_format
    )
    return teams_data, prior_by_id


async def _load_priors(
    db, rostered_ids: list[str], scoring_format: str = "ppr"
) -> dict[str, Optional[float]]:
    """Preseason prior ppg per rostered player.

    FIELD SELECTION (founder decision, signed off): ALWAYS prefer the Sonnet
    FORWARD projection ``projected_ppr_season`` over the backward historical
    ``ppr_points``, falling back to the historical number only when the projection
    is absent. The forward projection incorporates role/team/age change (a stronger
    prior than raw history, which shifts too much year-to-year); this also means a
    rookie prefers its Sonnet rookie projection over the raw comp average where both
    exist. Both fields are SEASON TOTALS on the SAME ``clean_season_baseline`` dict,
    so the ÷17 → PPG conversion is unchanged.

    ``scoring_format`` re-scores the (PPR) prior TOTAL into the league's format via
    ``scoring.season_points`` using the baseline's ``projected_receptions`` — exact,
    consistent with the live weekly re-score. PPR is the identity (byte-identical).

    Players with no profile row are absent from ``rows`` and keep their ``None``
    (null prior → prior_weight 0) — e.g. K/DEF, which have no profile at all
    (handled by separate arc pieces, NOT here). A player that HAS a profile but
    neither usable field (e.g. a depth profile with an empty baseline) is loud-
    warned — never silently dropped."""
    from sqlalchemy import select

    from backend.models.player import PlayerProfile
    from backend.scoring import season_points

    prior_by_id: dict[str, Optional[float]] = {pid: None for pid in rostered_ids}
    if not rostered_ids:
        return prior_by_id
    rows = (await db.execute(
        select(PlayerProfile.player_id, PlayerProfile.clean_season_baseline)
        .where(PlayerProfile.player_id.in_(rostered_ids))
    )).all()
    no_prior: list[str] = []
    for pid, baseline in rows:
        # Prefer the forward projection; fall back to the historical total.
        prior_total = None
        receptions = 0.0
        if isinstance(baseline, dict):
            prior_total = baseline.get("projected_ppr_season") or baseline.get("ppr_points")
            receptions = baseline.get("projected_receptions") or 0.0
        if prior_total:
            # Re-score the PPR prior total into the league format (identity for PPR).
            fmt_total = season_points(float(prior_total), float(receptions), scoring_format)
            prior_by_id[str(pid)] = fmt_total / 17.0
        else:
            no_prior.append(str(pid))
    if no_prior:
        logger.warning(
            "prior: %d profiled player(s) have neither projected_ppr_season nor "
            "ppr_points — no preseason prior (prior_weight=0): %s",
            len(no_prior), no_prior[:15],
        )
    return prior_by_id


# --- per-process seed cache (demo-only; teardown removes with the rest) -------
# The demo is a DETERMINISTIC pinned snapshot (static DEMO_ROSTERS + the
# DEMO_SEASON/DEMO_CURRENT_WEEK pin + parquet-cached weekly data), yet every
# request re-ran the full seed (~7s measured: 159 fuzzy name resolutions + the
# weekly build + evaluate_league) — the whole reason the trade/waiver pages were
# slow. Cache the built source per process: every state dataclass is frozen, so
# sharing one instance across requests is safe. Keyed by the (season, week) pin
# so a knob change can't serve a stale week (and --reload resets the module —
# and thus the cache — on any code edit anyway). Injected ``weekly_usage``
# (tests) BYPASSES the cache entirely. Grep TRADE_DEMO to tear down.
_SEED_CACHE: dict[tuple[int, int, str], TradeDemoSource] = {}
# Single-flight guard. asyncio.Lock binds to the first loop that awaits it (and
# raises from another loop — pytest creates a loop per test), so keep one lock
# PER RUNNING LOOP rather than one module-level lock.
_SEED_LOCKS: dict[int, asyncio.Lock] = {}


def _seed_lock(locks: dict[int, asyncio.Lock]) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    lock = locks.get(loop_id)
    if lock is None:
        lock = locks[loop_id] = asyncio.Lock()
    return lock


def clear_demo_seed_cache() -> None:
    """Drop the cached seed (tests / CLI after DB changes)."""
    _SEED_CACHE.clear()


async def seed_demo_league(
    db, *, scoring_format: str = "ppr", weekly_usage: Optional[pd.DataFrame] = None
) -> TradeDemoSource:
    """Build the demo source from the REAL DB + per-week layer — cached per process
    (see _SEED_CACHE). ``scoring_format`` re-scores the live weekly points + the prior
    into the league's format (PPR is byte-identical). ``weekly_usage`` can be injected
    (tests) to avoid the live fetch; the injected path is never cached."""
    if weekly_usage is not None:
        return await _seed_demo_league_uncached(db, weekly_usage, scoring_format)
    key = (DEMO_SEASON, DEMO_CURRENT_WEEK, scoring_format)
    if (hit := _SEED_CACHE.get(key)) is not None:
        return hit
    async with _seed_lock(_SEED_LOCKS):
        if (hit := _SEED_CACHE.get(key)) is not None:   # lost the race — reuse
            return hit
        src = await _seed_demo_league_uncached(db, None, scoring_format)
        _SEED_CACHE[key] = src
        logger.info("demo seed cached (per-process) for season=%d week=%d format=%s", *key)
        return src


async def _seed_demo_league_uncached(
    db, weekly_usage: Optional[pd.DataFrame], scoring_format: str = "ppr"
) -> TradeDemoSource:
    """The real build. Starter slots are re-derived from the engine's forward
    value, so the lineup reflects in-season production — not draft order."""
    teams_data, prior_by_id = await _resolve_rosters(db, scoring_format)
    priors = build_priors(prior_by_id)
    if weekly_usage is None:
        from backend.integrations.nfl_weekly import weekly_player_usage
        from backend.services.kdef_scoring import weekly_kdef_value_frame
        weekly_usage = await weekly_player_usage(
            DEMO_SEASON, weeks=range(1, DEMO_CURRENT_WEEK + 1), db=db,
        )
        # Un-strip (slice 3): union the scored K/DST weekly rows so the K/DST now on
        # the demo rosters value the SAME way offense does. Additive — the offense
        # rows are untouched, so offense values don't move.
        kdef = await weekly_kdef_value_frame(
            DEMO_SEASON, weeks=range(1, DEMO_CURRENT_WEEK + 1), db=db,
        )
        if kdef is not None and not kdef.empty:
            weekly_usage = pd.concat([weekly_usage, kdef], ignore_index=True)

    # Forward value first (slot-agnostic), then slot the lineup by that value.
    provisional = build_league_state(teams_data)
    values = evaluate_league(provisional, weekly_usage, scoring_format=scoring_format, priors=priors)
    value_by_id = {pid: v.forward_value for pid, v in values.items()}
    for team in teams_data:
        assign_starter_slots(team["players"], value_by_id)

    state = build_league_state(teams_data)
    return TradeDemoSource(
        state=state, weekly_usage=weekly_usage, priors=priors, scoring_format=scoring_format,
    )


async def maybe_demo_league_source(db) -> Optional[TradeDemoSource]:
    """THE GATE. Returns the demo source ONLY when TRADE_DEMO_MODE is on;
    otherwise None (the prod path selects the real provider, untouched here)."""
    if not trade_demo_enabled():
        return None
    return await seed_demo_league(db)
