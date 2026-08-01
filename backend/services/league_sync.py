"""
LeagueSyncService — unified sync across all platforms.

Imports league settings, draft history, current rosters,
and free agents. All synced data scoped to user_id.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.integrations.platform_factory import get_platform_api
from backend.models.user_league import UserLeague
from backend.repositories.league_repo import LeagueRepository
from backend.utils.seasons import get_current_season

logger = logging.getLogger(__name__)

# How many historical seasons to import
HISTORY_SEASONS = settings.league_sync_history_seasons


def _norm_owner(x) -> str:
    """Normalise an owner identity token for comparison. ESPN SWID/owners carry braces
    (`{GUID}`); casing varies. Sleeper user_ids are plain. Strip braces + upper-case."""
    return str(x or "").strip().strip("{}").upper()


def bind_my_team_id(rosters, identity: str | None) -> str | None:
    """The user's OWN team_id by EXACT owner-identity — never position or name.

    1. A platform SERVER-TAG wins first (Yahoo ``is_owned_by_current_login`` → the
       TeamRoster.is_me flag).
    2. Else the user's stored identity (Sleeper user_id / ESPN SWID) is matched against
       each roster's ``owner_ids`` (which INCLUDE co-owners) — so a co-owned team binds.

    Returns None when nothing matches — the caller fails LOUD and leaves is_me unbound;
    a no-match must NEVER fall back to a positional guess (team[0])."""
    for r in rosters:
        if getattr(r, "is_me", None):                      # Yahoo authoritative flag
            return str(r.platform_team_id)
    if identity:
        nid = _norm_owner(identity)
        for r in rosters:
            if nid in {_norm_owner(o) for o in (getattr(r, "owner_ids", None) or [])}:
                return str(r.platform_team_id)
    return None


def _manager_for(manager_map: dict | None, team_id) -> str:
    """Resolve a draft pick's team id to a manager name.

    Direct hit first — ESPN (bare team id) and Sleeper (bare roster_id) key
    manager_map with exactly the value their picks carry, so they match outright.

    Yahoo does NOT. manager_map is built from the CURRENT season's roster call,
    whose team keys are "<game_key>.l.<league>.t.<n>" with the game key for
    THIS season (470 in 2026). Historical draft results carry that season's game
    key instead (461 for 2025, 449 for 2024 — see YAHOO_NFL_GAME_KEYS). So a
    direct lookup misses on every historical Yahoo row.

    The league and team number are stable across seasons; only the game-key
    prefix rotates. So fall back to matching on the ".l.<league>.t.<n>" tail.
    That is still exact — it is the same league and the same team slot — it just
    ignores the season prefix.

    Returns "" on any miss. Never guesses: a wrong manager name is worse than a
    blank one, because build_manager_profiles uses it as a cross-season identity
    key.
    """
    if not manager_map or not team_id:
        return ""
    key = str(team_id)
    if key in manager_map:
        return manager_map[key] or ""
    # Yahoo cross-season: compare the league+team tail, ignoring the game key.
    if ".l." in key and ".t." in key:
        tail = key.split(".", 1)[1]           # "l.<league>.t.<n>"
        for k, v in manager_map.items():
            ks = str(k)
            if ".l." in ks and ks.split(".", 1)[1] == tail:
                return v or ""
    return ""


class LeagueSyncService:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID):
        self._db = db
        self._user_id = user_id
        self._league_repo = LeagueRepository(db)

    async def sync_league(
        self,
        user_league_id: uuid.UUID,
        league_key: str | None = None,
    ) -> dict:
        """
        Full sync for a connected league.

        Accepts UUID, reloads the ORM object using the service's
        own session to avoid detached/expired instance errors.
        """
        from backend.integrations.yahoo_api import yahoo_league_key

        # Reload within THIS session — not the router's
        user_league = await self._league_repo.get_user_league(
            self._user_id, user_league_id
        )
        if not user_league:
            from backend.core.exceptions import NotFoundError
            raise NotFoundError(
                f"League {user_league_id} not found"
            )

        platform = await get_platform_api(user_league, self._db)
        current_season = get_current_season()

        # 0. Fetch and store platform-specific league settings
        if user_league.platform == "yahoo":
            await self._sync_yahoo_settings(user_league, league_key)  # sets roster_slots
        elif user_league.platform == "espn":
            try:
                draft_type, budget = await platform.detect_draft_type()
                if user_league.draft_type != draft_type:
                    logger.info(
                        "ESPN draft type updated: %s → %s",
                        user_league.draft_type, draft_type,
                    )
                # Always write both: draft_type from the authoritative draftSettings.type,
                # and budget from it too — None for snake CLEARS a stale auction budget
                # (was only overwritten when non-None, so snake leagues kept a bogus 200).
                user_league.draft_type = draft_type
                user_league.budget = budget
            except Exception as exc:
                logger.warning("ESPN draft type re-detection failed: %s", exc)
            # T3 lineup config (defensive/sample-gated — unknown id → default).
            try:
                # Assign only on success. get_roster_slots() returns None on ANY
                # parse failure or unknown slot id (a deliberate fail-safe), so an
                # unconditional assignment let one transient blip on an unattended
                # passive sync wipe a correct, previously-synced lineup and silently
                # fall back to the generic default.
                _slots = await platform.get_roster_slots()
                if _slots is not None:
                    user_league.roster_slots = _slots
                else:
                    logger.warning(
                        "ESPN roster-slots returned None for league %s — keeping "
                        "the previously synced lineup", user_league.id,
                    )
            except Exception as exc:
                logger.warning("ESPN roster-slots sync failed: %s", exc)
            # Name / scoring / draft_date from the SAME mSettings response (stop discarding).
            try:
                self._apply_league_metadata(user_league, await platform.get_league_metadata())
            except Exception as exc:
                logger.warning("ESPN league-metadata sync failed: %s", exc)
        elif user_league.platform == "sleeper":
            # T3 lineup config from /v1/league roster_positions (verified live).
            try:
                # Assign only on success. get_roster_slots() returns None on ANY
                # parse failure or unknown slot id (a deliberate fail-safe), so an
                # unconditional assignment let one transient blip on an unattended
                # passive sync wipe a correct, previously-synced lineup and silently
                # fall back to the generic default.
                _slots = await platform.get_roster_slots()
                if _slots is not None:
                    user_league.roster_slots = _slots
                else:
                    logger.warning(
                        "Sleeper roster-slots returned None for league %s — keeping "
                        "the previously synced lineup", user_league.id,
                    )
            except Exception as exc:
                logger.warning("Sleeper roster-slots sync failed: %s", exc)
            # Name / scoring / draft_type / draft_date from /league + /drafts.
            try:
                self._apply_league_metadata(user_league, await platform.get_league_metadata())
            except Exception as exc:
                logger.warning("Sleeper league-metadata sync failed: %s", exc)

        summary = {
            "platform": user_league.platform,
            "league_id": user_league.league_id,
            "picks_imported": 0,
            "players_resolved": 0,
            # Picks the database already had. Surfaced so a RE-sync reads as
            # "nothing new" instead of looking like a failed import now that
            # picks_imported counts real writes rather than attempts.
            "picks_already_present": 0,
            "seasons_imported": 0,
            "managers_found": 0,
            "free_agents_cached": 0,
            "warnings": [],
        }

        # 1. Import current rosters — required, fail hard.
        # A league we cannot read rosters for is not synced in any sense.
        rosters = await platform.get_rosters()
        summary["managers_found"] = len(rosters)
        # manager_name OR team_name — ESPN leaves manager_name blank but carries the
        # real team_name, so this stores a real name for every platform (fixes the
        # ESPN all-blank opponent-names bug).
        user_league.manager_map = {
            r.platform_team_id: (r.manager_name or r.team_name or "")
            for r in rosters
        }
        # Real team count from the actual roster list (was stuck at connect default 12
        # for ESPN/Sleeper; Yahoo sets it from settings).
        if rosters and user_league.platform in ("espn", "sleeper"):
            user_league.team_count = len(rosters)

        # is_me BINDING — the user's OWN team by EXACT owner-identity, recomputed on
        # EVERY sync (self-heals across co-owner adds / manager swaps / ESPN team-index
        # reindex). NEVER positional. None → loud-warn + unbound (downstream fails safe,
        # never guesses a team). A binding failure never crashes the sync.
        try:
            identity = await self._platform_identity(user_league.platform)
            bound = bind_my_team_id(rosters, identity)
            if bound is None:
                logger.warning(
                    "league %s (%s): user identity matched NO team's owner — is_me UNBOUND "
                    "(no positional fallback). had_identity=%s teams=%d",
                    user_league.id, user_league.platform,
                    bool(identity) or any(r.is_me for r in rosters), len(rosters),
                )
            # CLOBBER PROTECTION + PRECEDENCE: a MANUAL pick is authoritative. A later
            # sync's auto-bind never overwrites it (else the recovery silently undoes
            # itself and the user is stuck again). If a SUCCESSFUL auto-bind DISAGREES
            # with the manual pick, we surface it loudly but KEEP the manual pick — the
            # user is the authority on their own identity (and the exact-match binder can
            # still land on a co-owner / shared identity). Auto/unset leagues bind normally.
            if user_league.my_team_id_source == "manual":
                if bound is not None and str(bound) != str(user_league.my_team_id):
                    logger.warning(
                        "league %s (%s): auto-bind resolved team %s but the user's MANUAL "
                        "pick is %s — KEEPING the manual pick (user is authoritative).",
                        user_league.id, user_league.platform, bound, user_league.my_team_id,
                    )
                # manual my_team_id + source unchanged
            else:
                user_league.my_team_id = bound
                user_league.my_team_id_source = "auto" if bound is not None else None
        except Exception as exc:
            logger.warning(
                "league %s (%s): is_me binding failed (%s) — left unbound",
                user_league.id, user_league.platform, exc,
            )

        # is_active SELF-HEAL: recompute from the season on EVERY sync (initial +
        # re-sync). Previously set once at connect and never touched here, so it went
        # stale over the calendar rollover and a re-sync couldn't repair it.
        user_league.is_active = (user_league.season_year == current_season)

        # 2. Stamp last_synced NOW. Draft history is optional context —
        # a new league with no draft yet must still read as synced.
        user_league.last_synced = datetime.now(timezone.utc)
        await self._db.commit()

        # 3. Import draft history — best-effort.
        #
        # ONLY YAHOO IS ADDRESSABLE BY SEASON. yahoo_league_key() puts that season's
        # game key in front of the league id (461.l.<id> is 2025, 423.l.<id> is 2023),
        # so each pass of the loop asks Yahoo for a genuinely different draft.
        #
        # ESPN AND SLEEPER ARE NOT. ESPNLeagueAPI.get_draft_picks ignores the
        # league_key argument and calls _get("mDraftDetail"), whose URL is built from
        # self._league.season_year. SleeperLeagueAPI.get_draft_picks ignores it too and
        # walks /league/{league_id}/drafts, and one Sleeper league id IS one season.
        # Both therefore return THE SAME current draft on every pass. Looping them
        # stored that one draft under four PAST years and never under the year it
        # belongs to, while reporting "seasons_imported: 4" for a league that has
        # drafted once.
        #
        # So Yahoo walks back and everyone else imports ONCE, stamped with the season
        # the league itself records. For ESPN that is the same value its own request
        # URL is built from, so the stored year and the fetched year cannot disagree.
        # It is also what supports deliberately connecting a past season: the connect
        # endpoints accept an explicit season (backend/routers/league_connect.py), and
        # an ESPN league connected as 2024 must fetch 2024 AND be stamped 2024.
        if user_league.platform == "yahoo":
            seasons = [
                s for s in (
                    current_season - offset - 1      # completed seasons only
                    for offset in range(HISTORY_SEASONS)
                ) if s >= 2020
            ]
        else:
            seasons = [user_league.season_year]

        picks_total = 0
        resolved_total = 0
        dropped_total = 0
        seasons_ok = 0
        for season in seasons:
            # Season-specific league key for Yahoo ONLY — the other two adapters
            # ignore this argument, which is exactly why they must not be looped.
            season_key = None
            if user_league.platform == "yahoo":
                season_key = yahoo_league_key(
                    user_league.league_id, season
                )

            try:
                logger.info(
                    "Fetching draft picks: platform=%s key=%s season=%d",
                    user_league.platform,
                    season_key or user_league.league_id,
                    season,
                )
                picks = await platform.get_draft_picks(
                    league_key=season_key
                )
                logger.info(
                    "Got %d picks for season %d", len(picks), season
                )
                if picks:
                    res = await self._store_picks(
                        picks, user_league.id, season,
                        manager_map=user_league.manager_map,
                        platform=user_league.platform,
                    )
                    picks_total += res["stored"]
                    resolved_total += res["resolved"]
                    dropped_total += res.get("dropped", 0)
                    seasons_ok += 1
                    # A draft whose players we could not identify is unusable downstream
                    # (the backtest matches on name then player_id), so say so loudly
                    # rather than reporting a healthy-looking pick count.
                    if res["stored"] and res["resolved"] < res["stored"] * 0.5:
                        msg = (
                            f"{season}: only {res['resolved']}/{res['stored']} picks "
                            "matched a known player — prices will not be usable"
                        )
                        logger.warning("league_sync: %s", msg)
                        summary["warnings"].append(msg)
            except Exception as exc:
                logger.warning(
                    "Could not import %s season %d: %s",
                    user_league.platform, season, exc,
                )
                summary["warnings"].append(
                    f"No draft history for {season}"
                )
                # Rollback so the transaction isn't permanently aborted
                await self._db.rollback()

        summary["picks_imported"] = picks_total
        summary["players_resolved"] = resolved_total
        summary["picks_already_present"] = dropped_total
        summary["seasons_imported"] = seasons_ok

        # 4. Cache free agents count — best-effort
        try:
            free_agents = await platform.get_free_agents()
            summary["free_agents_cached"] = len(free_agents)
        except Exception as exc:
            logger.warning("Could not cache free agents: %s", exc)
            summary["warnings"].append("Free agent sync failed")

        await self._db.commit()
        return summary

    async def _platform_identity(self, platform: str) -> str | None:
        """The user's stored platform identity for id-matching a team owner. Yahoo returns
        None — it binds on the per-team ``is_owned_by_current_login`` flag, not a stored id."""
        from backend.repositories.credential_repo import CredentialRepository
        repo = CredentialRepository(self._db)
        if platform == "sleeper":
            cred = await repo.get_for_user(self._user_id, "sleeper")
            return getattr(cred, "sleeper_user_id", None) if cred else None
        if platform == "espn":
            cookies = await repo.get_espn_cookies(self._user_id)
            return cookies[1] if cookies else None   # SWID
        return None

    def _apply_league_metadata(self, user_league: UserLeague, meta) -> None:
        """Store the non-None fields of a LeagueMetadata onto the league. None means
        the platform didn't expose it → keep the existing value (never clobber with a
        default)."""
        if meta.name:
            user_league.league_name = meta.name
        if meta.scoring:
            user_league.scoring = meta.scoring
        if meta.team_count:
            user_league.team_count = meta.team_count
        if meta.draft_type:
            user_league.draft_type = meta.draft_type
        if meta.draft_date:
            user_league.draft_date = meta.draft_date
        if meta.draft_status:
            user_league.draft_status = meta.draft_status
        # `is not None`, NOT truthiness: uses_bidding_budget is a tri-state and
        # False is a real, meaningful answer ("this league does not bid"). A
        # truthiness test would silently discard it and leave the league looking
        # like its waiver system is unknown.
        if meta.uses_bidding_budget is not None:
            user_league.uses_bidding_budget = meta.uses_bidding_budget
        if meta.waiver_budget is not None:
            user_league.waiver_budget = meta.waiver_budget
        if meta.waiver_system:
            user_league.waiver_type = meta.waiver_system

    async def _sync_yahoo_settings(
        self, user_league: UserLeague, league_key: str | None = None,
    ) -> None:
        """Fetch Yahoo league settings and update user_league record."""
        try:
            from backend.integrations.yahoo_api import (
                get_league_settings,
                refresh_access_token_for_user,
                yahoo_league_key,
            )
            from backend.repositories.credential_repo import CredentialRepository

            key = league_key or yahoo_league_key(
                user_league.league_id, user_league.season_year
            )
            repo = CredentialRepository(self._db)
            tokens = await repo.get_yahoo_tokens(self._user_id)
            if not tokens:
                logger.warning(
                    "No Yahoo tokens for user %s — skipping settings sync",
                    self._user_id,
                )
                return

            access_token, refresh_token, expires_at = tokens
            if expires_at and datetime.now(timezone.utc) >= expires_at:
                access_token, refresh_token, new_expiry = (
                    await refresh_access_token_for_user(refresh_token)
                )
                await repo.upsert_yahoo(
                    self._user_id, access_token, refresh_token, new_expiry,
                )

            settings = await get_league_settings(access_token, key)
            user_league.league_name = settings["name"]
            user_league.team_count = settings["num_teams"]
            user_league.draft_type = settings["draft_type"]
            user_league.scoring = settings["scoring_type"]
            user_league.budget = settings.get("auction_budget")
            # Per-league lineup (T3): authoritative when the settings parse; null →
            # default lineup. A re-sync updates it (idempotent).
            user_league.roster_slots = settings.get("roster_slots")
            # Settings get_league_settings already pulls but sync used to drop.
            if settings.get("draft_date"):
                user_league.draft_date = settings["draft_date"]
            user_league.trade_deadline = settings.get("trade_deadline") or None
            # waiver_type is a VARCHAR(30); Yahoo's raw rule string is not a value
            # we control, so cap it rather than letting an over-long one abort the
            # whole settings sync.
            _wt = settings.get("waiver_type") or None
            user_league.waiver_type = _wt[:30] if _wt else None
            # Tri-state, and `is not None` NOT truthiness: False is a real answer
            # ("this league does not bid") that a truthiness test would discard,
            # leaving the league looking unknown. None means Yahoo did not tell us,
            # and we leave whatever is already stored alone rather than overwriting
            # a known answer with a guess.
            _uses_faab = settings.get("uses_faab")
            if _uses_faab is not None:
                user_league.uses_bidding_budget = bool(_uses_faab)
            # waiver_budget is intentionally NOT set here. No Yahoo field for a
            # league's FAAB budget has been confirmed against a live response, so
            # there is nothing to read; it stays NULL and the waiver page states its
            # $100 figure as an assumption instead of as the league's own number.
            user_league.playoff_start_week = settings.get("playoff_start_week")
            await self._db.flush()
            logger.info(
                "Yahoo settings synced: name=%s teams=%d draft=%s scoring=%s",
                settings["name"], settings["num_teams"],
                settings["draft_type"], settings["scoring_type"],
            )
        except Exception as exc:
            logger.warning("Could not fetch Yahoo league settings: %s", exc)

    @staticmethod
    def _pick_key(platform: str, platform_player_id: str | None) -> str:
        """The bare platform player id to match against the players table.

        Yahoo draft results return a compound key like "461.p.33963"; the
        players table stores only the numeric tail in Player.yahoo_id. ESPN and
        Sleeper both return the bare id already.
        """
        key = str(platform_player_id or "")
        if not key:
            return ""
        if platform == "yahoo":
            return key.rsplit(".p.", 1)[-1] if ".p." in key else ""
        return key

    async def _resolve_pick_identities(
        self, picks: list, platform: str = "yahoo",
    ) -> dict[str, tuple]:
        """Map each pick's platform player key to a real player row.

        WHY THIS EXISTS. Yahoo's ``draftresults`` endpoint returns only ``player_key``,
        ``pick``, ``round``, ``team_key`` and ``cost`` — no name, no position. The adapter
        therefore hands us ``player_name=""`` and ``position=""`` (yahoo_league_api.py,
        "Resolved separately if needed"), and nothing ever resolved them. The result was a
        ``league_auction_history`` that no consumer could use: the backtest matches by
        player_name, then by player_id, and BOTH were empty on every row of every season,
        so it silently fell through to ``market_value_historic`` — a table whose only
        writer snapshots FantasyPros consensus. A real auction sat unused for two months.

        Keys look like ``461.p.33963``; the suffix is the Yahoo player id, which is what
        ``players.yahoo_id`` stores. One query for the whole draft, never one per pick.

        AMBIGUOUS KEYS ARE DROPPED, NOT GUESSED. There are duplicate player rows in this
        database (18 yahoo_ids map to more than one row), and binding a price to the wrong
        one is worse than leaving it unresolved — it would silently attribute a real
        auction price to the wrong player. Consistent with the ID-first matching rule:
        never resolve on a key that does not identify exactly one player.
        """
        from sqlalchemy import select

        from backend.models.player import Player

        col = {
            "yahoo": Player.yahoo_id,
            "espn": Player.espn_id,
            "sleeper": Player.sleeper_id,
        }.get(platform)
        if col is None:
            logger.warning(
                "league_sync: no identity column for platform %r — draft picks "
                "will store without a player_id", platform,
            )
            return {}

        ids = {
            k for k in (
                self._pick_key(platform, p.platform_player_id) for p in picks
            ) if k
        }
        if not ids:
            return {}

        rows = (await self._db.execute(
            select(Player.id, col.label("pid"), Player.name, Player.position)
            .where(col.in_(ids))
        )).all()

        seen: dict[str, tuple] = {}
        ambiguous: set[str] = set()
        for row in rows:
            if row.pid in seen:
                ambiguous.add(row.pid)
                continue
            seen[row.pid] = (row.id, row.name, row.position)
        for dup in ambiguous:
            seen.pop(dup, None)
        if ambiguous:
            logger.warning(
                "league_sync: %d yahoo_id(s) matched multiple player rows — left "
                "unresolved rather than bound to a guess: %s",
                len(ambiguous), sorted(ambiguous)[:5],
            )
        return seen

    async def _store_picks(
        self,
        picks: list,
        user_league_id: uuid.UUID,
        season: int,
        manager_map: dict | None = None,
        platform: str = "yahoo",
    ) -> dict:
        """
        Store historical draft picks, resolving player identity as we go.
        All picks scoped to user_id + user_league_id.
        Deduplication via on_conflict_do_nothing.

        Returns {"stored": n, "resolved": n} — the resolved count is reported by the
        caller because a silent 0% is the exact failure this method used to have.

        ``manager_map`` fills manager_name, which ALL THREE adapters hardcode to ""
        on draft picks (yahoo_league_api.py:251, espn_league_api.py:276,
        sleeper_league_api.py:165) even though every one of them resolves real names
        on the ROSTER path. LeagueAuctionHistoryRepository.manager_tendencies filters
        `manager_name != ""`, so opponent tendencies were permanently empty for every
        synced user — the map was built ~70 lines earlier in this same sync and simply
        never handed down.

        The join is safe on all three platforms because manager_map is keyed on
        TeamRoster.platform_team_id and that is the SAME value as
        DraftPick.picked_by_team_id: Yahoo team_key both sides, ESPN str(team id)
        both sides, Sleeper roster_id both sides. Verified per adapter — a mismatch
        would attribute a real name to the wrong manager, which is worse than blank.
        """
        from backend.models.league_auction_history import LeagueAuctionHistory
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        identities = await self._resolve_pick_identities(picks, platform)

        count = 0
        resolved = 0
        dropped = 0
        for pick in picks:
            if not pick.player_name and not pick.platform_player_id:
                continue
            key = pick.platform_player_id or ""
            player_id, name, position = identities.get(
                self._pick_key(platform, key), (None, "", ""))
            result = await self._db.execute(
                pg_insert(LeagueAuctionHistory)
                .values(
                    user_id=self._user_id,
                    user_league_id=user_league_id,
                    player_id=player_id,
                    # The platform's own value still wins when it supplies one; the
                    # resolved row is a fallback, not an override.
                    player_name=pick.player_name or name or "",
                    position=pick.position or position or "",
                    price=pick.auction_price or 0,
                    # Adapter value wins if it ever supplies one; otherwise fall
                    # back to the roster-derived map. Today every adapter sends "",
                    # so the map is what actually populates this.
                    manager_name=(
                        pick.manager_name
                        or _manager_for(manager_map, pick.picked_by_team_id)
                        or ""
                    ),
                    draft_pick_number=pick.pick_number,
                    season_year=season,
                    # The user_league id makes this unique PER CUSTOMER.
                    # Neither unique constraint on this table carries a customer
                    # or league column, so `source` is the only thing separating
                    # one customer's draft from another's. It used to be just the
                    # team id, which is "1".."12" on ESPN and Sleeper, so two
                    # customers collided and the second one's picks were silently
                    # discarded by ON CONFLICT DO NOTHING. Yahoo was not immune
                    # either: two customers in the SAME Yahoo league produced
                    # identical values, and production already has two users
                    # sharing Yahoo league 141688.
                    source=f"sync_{user_league_id}_{pick.picked_by_team_id}",
                    yahoo_player_key=pick.platform_player_id or None,
                )
                .on_conflict_do_nothing()
            )
            # COUNT WRITES, NOT ATTEMPTS. `count += 1` used to run unconditionally
            # right here, and `resolved += 1` ran BEFORE the insert, so both counters
            # reported attempts. A re-sync of an already-imported league reported a
            # full, healthy-looking import while inserting nothing at all — and the
            # same number is what hides a genuine cross-tenant collision, where
            # on_conflict_do_nothing silently discards a second customer's picks.
            #
            # `!= 0` rather than `+= result.rowcount`: on a single-row execute with an
            # untargeted ON CONFLICT DO NOTHING the driver reports exactly 0 or 1, and
            # treating a -1 "unknown" as written preserves the old over-count rather
            # than silently reporting zero imports.
            if result.rowcount != 0:
                count += 1
                if player_id is not None:
                    resolved += 1
            else:
                dropped += 1

        await self._db.commit()
        # `resolved` MUST be counted inside the written branch: the caller compares it
        # against `stored` to decide whether to warn that prices are unusable, and a
        # ratio built from one counter of writes and one of attempts can exceed 1 on a
        # re-sync, which silences that warning exactly when it matters.
        return {"stored": count, "resolved": resolved, "dropped": dropped}
