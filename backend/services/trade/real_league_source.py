"""
RealLeagueSource — turn a SYNCED real league into a populated LeagueState the value
engine consumes. The permanent replacement for the demo scaffolding: it produces the
SAME LeagueState shape (so evaluate_league / apply_dst_matchup / the trade+waiver+
matchup surfaces run unchanged), but from a real user's synced platform rosters.

Resolution is DETERMINISTIC via the canonical resolve_player (#243) — NOT the demo's
name-fuzzy _resolve_rosters template. Per platform (recon V1b):
  * Sleeper: platform_player_id IS the sleeper_id (DST id is the team abbr, which
    resolves id-native against DEF rows) → resolve_player(sleeper_id=…).
  * ESPN: platform_player_id is the ESPN player id → resolve_player(espn_id=…);
    DST carries no player id, so it routes by team (populated from proTeamId).
  * Yahoo: platform_player_id is the full player_key "NNN.p.12345" → NORMALIZED to
    the bare "12345" (what #243 stored) → resolve_player(yahoo_id=…). Passing the
    raw key would miss every Yahoo id and collapse to guarded-name.
DST is detected FIRST (position==DEF) and routed to the team map, never id/fuzzy.
name + position are ALWAYS passed so the fringe id-miss tail falls to GUARDED name
(position filter + collision refusal), never bare. Unresolved players are loud-warned
with their identifying info and surfaced as a count — never a silent drop.

The free-agent pool is DERIVED (platform-agnostic, recon V2): the in-season-active
rosterable universe MINUS every player rostered on ANY team in THIS league, diffed by
canonical_player_id (post-resolution) — never by name.

Injectable (``team_rosters`` / ``weekly_usage``) so it's testable + verifiable against
real records without live platform creds.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
from sqlalchemy import select

from backend.core.exceptions import UnboundTeamError, UndraftedLeagueError
from backend.integrations.platform_models import TeamRoster
from backend.models.player import Player
from backend.repositories.player_repo import PlayerRepository
from backend.services.trade.league_state import LeagueState, RosterPlayer, TeamState
from backend.services.trade.trade_analysis import DEFAULT_ROSTER_LIMIT
from backend.utils.seasons import get_current_nfl_week, get_current_season

logger = logging.getLogger(__name__)

# FA-pool universe filter — a rosterable free agent is a skill/K/DEF player with at
# least this many played games in the current season (streamable relevance; trims
# one-week blips). K/DEF included — they're streamed. NOT all ~4300 rows.
_POOL_MIN_GAMES = 3
_SKILL = ("QB", "RB", "WR", "TE", "K", "DEF")


@dataclass
class RealLeagueSource:
    """A LeagueStateProvider (same Protocol as the demo source) plus the real
    season/week + derived free-agent pool + the loud-warned unresolved set."""
    state: LeagueState
    weekly_usage: pd.DataFrame
    priors: dict[str, float]
    season: int
    week: int
    roster_limit: int
    pool: list[RosterPlayer] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    my_team_id: Optional[str] = None

    def get_league_state(self) -> LeagueState:
        return self.state


# ---------------------------------------------------------------------------
# resolution helpers (pure where possible)
# ---------------------------------------------------------------------------
def normalize_yahoo_id(player_key: str | None) -> str | None:
    """Yahoo roster player_key "449.p.12345" → the bare "12345" that #243 stored as
    ``players.yahoo_id``. Returns None for a blank/garbage key."""
    if not player_key:
        return None
    tail = str(player_key).strip().split(".")[-1].strip()
    return tail or None


def _resolver_kwargs(platform: str, rp) -> dict:
    """Map one platform RosteredPlayer to resolve_player kwargs. name/position/team
    ALWAYS included (so an id-miss falls to GUARDED name, not bare). DST (position
    ==DEF) carries no id → team-routed by resolve_player."""
    base = {
        "name": rp.player_name or None,
        "position": rp.position or None,
        "team": rp.team_abbr or None,
    }
    if (rp.position or "").upper() == "DEF":
        return base  # position=DEF → find_by_dst_team(team or name), never id/fuzzy
    pid = rp.platform_player_id
    if platform == "sleeper":
        base["sleeper_id"] = pid           # DST here has position="" but pid==abbr → id-native
    elif platform == "espn":
        base["espn_id"] = pid
    elif platform == "yahoo":
        base["yahoo_id"] = normalize_yahoo_id(pid)
    return base


async def resolve_team_rosters(
    db, platform: str, team_rosters: list[TeamRoster], my_team_id: Optional[str],
) -> tuple[list[TeamState], list[dict]]:
    """Resolve every roster entry to a canonical Player via resolve_player. Returns
    (teams, unresolved). Unresolved players are loud-warned + collected, never
    silently dropped."""
    repo = PlayerRepository(db)
    teams: list[TeamState] = []
    unresolved: list[dict] = []
    for tr in team_rosters:
        players: list[RosterPlayer] = []
        for rp in tr.players:
            player = await repo.resolve_player(**_resolver_kwargs(platform, rp))
            if player is None:
                info = {
                    "platform": platform, "team": tr.team_name or tr.manager_name,
                    "name": rp.player_name, "position": rp.position,
                    "platform_id": rp.platform_player_id, "nfl_team": rp.team_abbr,
                }
                unresolved.append(info)
                logger.warning(
                    "real league: UNRESOLVED %s player name=%r pos=%r nfl_team=%r "
                    "platform_id=%r on %r — dropped (not added to roster)",
                    platform, rp.player_name, rp.position, rp.team_abbr,
                    rp.platform_player_id, info["team"],
                )
                continue
            players.append(RosterPlayer(
                canonical_player_id=str(player.id), name=player.name,
                position=player.position, nfl_team=player.team_abbr,
                injury_status=rp.injury_status, starter_slot=None,
            ))
        teams.append(TeamState(
            team_id=str(tr.platform_team_id),
            team_name=tr.team_name or tr.manager_name or str(tr.platform_team_id),
            is_me=(my_team_id is not None and str(tr.platform_team_id) == str(my_team_id)),
            roster=tuple(players),
        ))
    return teams, unresolved


def _roster_limit_from_slots(roster_slots: dict | None) -> int:
    """Real league roster size = the sum of every slot count (starters + bench).
    None (unsynced) → the shared default."""
    if not roster_slots:
        return DEFAULT_ROSTER_LIMIT
    total = sum(int(v or 0) for v in roster_slots.values())
    return total or DEFAULT_ROSTER_LIMIT


async def _fetch_weekly(db, season: int, week: int) -> pd.DataFrame:
    """Real per-week usage (offense + scored K/DST) for weeks 1..week — the SAME
    frame the demo builds, but for the live season.

    A not-yet-started season has NO weekly data: get_current_nfl_week returns 0 in
    the offseason, and the nflverse fetchers 404 when asked for a season that hasn't
    published (e.g. 2026 in Aug 2026). That is a HANDLED no-data state, not an error
    — return an EMPTY frame so the value blend falls to prior-only. We never pretend
    data exists; the miss is loud-warned. (Undrafted leagues short-circuit before this
    via the undrafted guard; this still protects the drafted-but-pre-season window and
    any transient nflverse gap.)"""
    if week < 1:
        logger.info("real league: season %s week %s — no in-season data yet; "
                    "weekly frame is empty (prior-only value).", season, week)
        return pd.DataFrame()

    from backend.integrations.nfl_weekly import weekly_player_usage
    from backend.services.kdef_scoring import weekly_kdef_value_frame

    weeks = range(1, week + 1)
    try:
        weekly = await weekly_player_usage(season, weeks=weeks, db=db)
        kdef = await weekly_kdef_value_frame(season, weeks=weeks, db=db)
    except Exception as exc:  # nflverse 404 / no-data for a season that hasn't published
        logger.warning("real league: weekly-data fetch failed for season %s wk1-%s (%s: %s) "
                       "— degrading to an EMPTY frame (prior-only value), not a 500.",
                       season, week, type(exc).__name__, exc)
        return pd.DataFrame()
    if kdef is not None and not kdef.empty:
        weekly = pd.concat([weekly, kdef], ignore_index=True)
    return weekly


async def _derive_pool(db, weekly: pd.DataFrame, rostered_ids: set[str]) -> list[RosterPlayer]:
    """FA pool = in-season-active (_POOL_MIN_GAMES+ games) _SKILL players NOT rostered
    on ANY team in the league. Excluded by canonical_player_id (post-resolution),
    NEVER by name. K/DEF included (streamed)."""
    if weekly is None or getattr(weekly, "empty", True):
        return []
    counts = weekly.groupby("canonical_player_id").size()
    eligible = {str(pid) for pid, n in counts.items() if pid and n >= _POOL_MIN_GAMES}
    candidate_ids = eligible - rostered_ids
    if not candidate_ids:
        return []
    uids: list[uuid.UUID] = []
    for x in candidate_ids:
        try:
            uids.append(uuid.UUID(x))
        except (ValueError, TypeError):
            continue
    if not uids:
        return []
    rows = (await db.execute(
        select(Player.id, Player.name, Player.position, Player.team_abbr, Player.injury_status)
        .where(Player.id.in_(uids), Player.position.in_(_SKILL))
    )).all()
    return [
        RosterPlayer(canonical_player_id=str(pid), name=name, position=pos,
                     nfl_team=team, injury_status=inj)
        for pid, name, pos, team, inj in rows
    ]


def persisted_undrafted_signal(league) -> Optional[str]:
    """EXPLICIT undrafted signal from persisted sync fields — no live fetch, so it can
    short-circuit before the roster/weekly fetch (which also sidesteps the offseason
    no-data path). Priority: Sleeper draft_status, then a future draft_date
    (Yahoo/ESPN). Returns the signal name, or None when the league looks drafted /
    unknown (fall to the empty-roster inference after the sync)."""
    status = (getattr(league, "draft_status", None) or "").strip().lower()
    if status in ("pre_draft", "predraft", "drafting"):
        return "draft_status"
    if status == "complete":
        return None  # explicitly drafted — never infer over it
    draft_date = getattr(league, "draft_date", None)
    if draft_date is not None and draft_date > datetime.now(timezone.utc):
        return "draft_date"
    return None


async def _pick_league(db, user, user_league):
    """Resolve which synced league to analyze. Explicit ``user_league`` wins; else
    the user's first active league. Loud-warn on none/ambiguity."""
    if user_league is not None:
        return user_league
    from backend.repositories.league_repo import LeagueRepository

    leagues = await LeagueRepository(db).get_active_leagues(user.id)
    if not leagues:
        return None
    if len(leagues) > 1:
        logger.warning(
            "real league: user %s has %d active leagues — analyzing the first (%s); "
            "a league selector is a follow-up", user.id, len(leagues), leagues[0].id,
        )
    return leagues[0]


# ---------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------
async def build_real_league_source(
    db,
    user,
    *,
    user_league=None,
    team_rosters: Optional[list[TeamRoster]] = None,
    weekly_usage: Optional[pd.DataFrame] = None,
    my_team_id: Optional[str] = None,
) -> Optional[RealLeagueSource]:
    """Build a RealLeagueSource for a synced league. Returns None if the user has no
    synced league (caller raises the appropriate 4xx). ``team_rosters`` / ``weekly_usage``
    are injectable for tests; live otherwise. ``my_team_id`` binds the acting team
    (is_me) — until an own-team auto-binding lands (flagged follow-up), the request's
    my_team_id drives the acting team; is_me stays False when unbound."""
    league = await _pick_league(db, user, user_league)
    if league is None:
        return None

    # Injected rosters (tests / the simulated-drafted path) bypass BOTH undrafted
    # guards — the caller is supplying its own roster shape on purpose.
    injected = team_rosters is not None

    # UNDRAFTED GUARD (explicit): short-circuit BEFORE any roster/weekly fetch so an
    # undrafted league never hits the value path (no nonsense, and it sidesteps the
    # offseason no-data fetch entirely).
    if not injected:
        signal = persisted_undrafted_signal(league)
        if signal:
            logger.info("real league %s (%s): undrafted via %s — short-circuit to empty state",
                        league.id, league.platform, signal)
            raise UndraftedLeagueError(signal)

    # is_me comes from the league's stored binding (exact owner-identity, recomputed each
    # sync) unless the caller explicitly overrides it (the 'acting as' switcher). NULL
    # binding → is_me stays unbound; downstream fails loud, never guesses a team.
    if my_team_id is None:
        my_team_id = getattr(league, "my_team_id", None)

    season = league.season_year or get_current_season()
    week = get_current_nfl_week(season)

    if team_rosters is None:
        from backend.integrations.platform_factory import get_platform_api
        platform_api = await get_platform_api(league, db)
        team_rosters = await platform_api.get_rosters()

    teams, unresolved = await resolve_team_rosters(db, league.platform, team_rosters, my_team_id)
    if unresolved:
        total = sum(len(tr.players) for tr in team_rosters)
        logger.warning(
            "real league %s (%s): %d/%d roster players unresolved (dropped)",
            league.id, league.platform, len(unresolved), total,
        )

    # UNDRAFTED GUARD (inference fallback): no explicit signal fired, but every team's
    # roster came back empty. This is INFERENCE — a failed/partial sync looks identical
    # — so LOUD-WARN and use the hedged 'inferred' copy (never asserts undrafted as
    # certain). Only when the live fetch actually returned teams (not an injected path).
    if not injected and teams and not any(t.roster for t in teams):
        logger.warning(
            "real league %s (%s): NO explicit draft signal and ALL rosters empty — "
            "INFERRING undrafted (could also be a failed sync). Serving the empty state.",
            league.id, league.platform,
        )
        raise UndraftedLeagueError("inferred")

    # UNBOUND-TEAM GUARD: the league is drafted but exact-identity binding matched no
    # team (league.my_team_id is null and the caller gave no override). We must NOT
    # guess — instead hand the UI the team list so the USER picks their team (persisted
    # manually via PATCH /leagues/{id}/my-team). Keyed purely on my_team_id (which every
    # caller controls), so it's not gated on ``injected`` — an injected caller that wants
    # a bound team passes my_team_id.
    if my_team_id is None and teams:
        logger.info("real league %s (%s): identity unbound — offering team picker (%d teams)",
                    league.id, league.platform, len(teams))
        raise UnboundTeamError(
            league.id,
            [{"team_id": t.team_id, "name": t.team_name} for t in teams],
        )

    roster_slots = league.roster_slots
    roster_limit = _roster_limit_from_slots(roster_slots)

    if weekly_usage is None:
        weekly_usage = await _fetch_weekly(db, season, week)

    rostered_ids = {rp.canonical_player_id for t in teams for rp in t.roster}
    pool = await _derive_pool(db, weekly_usage, rostered_ids)

    from backend.services.trade.trade_demo_source import _load_priors, build_priors
    all_ids = list(rostered_ids | {rp.canonical_player_id for rp in pool})
    priors = build_priors(await _load_priors(db, all_ids))

    state = LeagueState(
        season=season, week=week, teams=tuple(teams), roster_slots=roster_slots,
    )
    return RealLeagueSource(
        state=state, weekly_usage=weekly_usage, priors=priors, season=season,
        week=week, roster_limit=roster_limit, pool=pool, unresolved=unresolved,
        my_team_id=my_team_id,
    )


@dataclass
class RealWaiverSource:
    """Waiver-shaped view of a real league (same attributes the waiver router reads
    off WaiverDemoSource): my rosters + the derived FA pool valued on ONE anchor
    basis, plus (defaulted) FAAB settings and the DST matchup context."""
    state: LeagueState
    pool: list[RosterPlayer]
    values: dict
    weekly_usage: pd.DataFrame
    priors: dict[str, float]
    roster_limit: int
    waiver_type: str = "faab"
    faab_budget: int = 100
    faab_remaining_by_team: dict = field(default_factory=dict)
    dst_matchup: dict = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)
    my_team_id: Optional[str] = None

    def get_league_state(self) -> LeagueState:
        return self.state


async def build_real_waiver_source(
    db,
    user,
    *,
    user_league=None,
    team_rosters: Optional[list[TeamRoster]] = None,
    weekly_usage: Optional[pd.DataFrame] = None,
    my_team_id: Optional[str] = None,
) -> Optional[RealWaiverSource]:
    """Real waiver source: build the league, then value roster + FA pool in ONE
    evaluate_league over an augmented LeagueState (a synthetic pool team) so every
    forward_value shares the anchor basis the add/drop math needs — mirroring the
    demo waiver source, on real data. FAAB defaults (UserLeague has no waiver
    columns yet — real FAAB sync is a follow-up)."""
    from backend.services.kdef_matchup import apply_dst_matchup
    from backend.services.trade.value_engine import evaluate_league
    from backend.services.waiver.faab import FAAB_BUDGET_DEFAULT

    source = await build_real_league_source(
        db, user, user_league=user_league, team_rosters=team_rosters,
        weekly_usage=weekly_usage, my_team_id=my_team_id,
    )
    if source is None:
        return None

    pool_team = TeamState(
        team_id="waiver-pool", team_name="Free Agents", is_me=False,
        roster=tuple(source.pool),
    )
    aug = LeagueState(
        season=source.season, week=source.week,
        teams=source.state.teams + (pool_team,), roster_slots=source.state.roster_slots,
    )
    values = evaluate_league(aug, source.weekly_usage, priors=source.priors)
    values, dst_matchup = apply_dst_matchup(values, aug, season=source.season, week=source.week)

    faab_remaining = {t.team_id: FAAB_BUDGET_DEFAULT for t in source.state.teams}
    return RealWaiverSource(
        state=source.state, pool=source.pool, values=values,
        weekly_usage=source.weekly_usage, priors=source.priors,
        roster_limit=source.roster_limit, faab_budget=FAAB_BUDGET_DEFAULT,
        faab_remaining_by_team=faab_remaining, dst_matchup=dst_matchup,
        unresolved=source.unresolved, my_team_id=source.my_team_id,
    )
