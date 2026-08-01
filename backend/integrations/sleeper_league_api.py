"""
Sleeper LeaguePlatformAPI implementation.
Public API — no auth required. Username only.
"""
from __future__ import annotations

import logging

import httpx

from datetime import datetime, timezone

from typing import Optional

from backend.integrations.platform_api import LeaguePlatformAPI
from backend.integrations.platform_models import (
    DraftPick, FreeAgent, LeagueMetadata, RosteredPlayer, TeamRoster,
    Transaction, WeeklyMatchup,
)
from backend.models.user_league import UserLeague

logger = logging.getLogger(__name__)

SLEEPER_BASE = "https://api.sleeper.app/v1"


def _scoring_from_rec(rec) -> str | None:
    """Points-per-reception → canonical scoring. None when not derivable."""
    try:
        r = float(rec)
    except (TypeError, ValueError):
        return None
    if r >= 1.0:
        return "ppr"
    if r >= 0.5:
        return "half_ppr"
    return "standard"


class SleeperLeagueAPI(LeaguePlatformAPI):
    """Sleeper Fantasy — public API, no auth required."""

    def __init__(self, league: UserLeague):
        self._league = league

    async def _get(self, path: str) -> dict | list:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{SLEEPER_BASE}{path}")
            resp.raise_for_status()
            return resp.json()

    async def get_roster_slots(self) -> dict | None:
        """Sleeper `/v1/league/{id}.roster_positions` (unauthenticated) → canonical
        {slot_type: count}. The array IS the token list; bench = explicit BN count.
        VERIFIED LIVE in recon. None on any failure → default lineup."""
        from backend.services.roster_slots import slots_from_sleeper_league
        try:
            lg = await self._get(f"/league/{self._league.league_id}")
        except Exception:
            return None
        if not isinstance(lg, dict):
            return None
        return slots_from_sleeper_league(
            lg.get("roster_positions"), league=str(self._league.league_id)
        )

    async def get_league_metadata(self) -> LeagueMetadata:
        """Sleeper `/league/{id}` (name, scoring_settings.rec, total_rosters) +
        `/league/{id}/drafts` (draft type + start_time) — the same objects sync
        already touches, previously mined only for roster_positions. Fails soft:
        any missing field stays None (won't overwrite)."""
        meta = LeagueMetadata()
        try:
            lg = await self._get(f"/league/{self._league.league_id}")
            if isinstance(lg, dict):
                meta.name = lg.get("name") or None
                meta.team_count = lg.get("total_rosters") or None
                meta.scoring = _scoring_from_rec((lg.get("scoring_settings") or {}).get("rec"))
                # Waiver settings from the SAME payload — no extra call.
                # settings.waiver_type is an integer: 2 means the league bids with
                # a budget; 0 and 1 are rolling waivers and reverse standings.
                # Gate on it BEFORE reading waiver_budget: measured across 285 live
                # leagues, waiver_budget is present on ALL of them including every
                # non-bidding league, almost always as a vestigial 100. Reading the
                # budget without this gate is how every league ends up claiming a
                # $100 bidding budget it does not have.
                _s = lg.get("settings") or {}
                _wt = _s.get("waiver_type")
                if _wt is not None:
                    try:
                        _wt = int(_wt)
                    except (TypeError, ValueError):
                        _wt = None
                if _wt is not None:
                    meta.uses_bidding_budget = (_wt == 2)
                    meta.waiver_system = (
                        "budget" if _wt == 2
                        else "reverse standings" if _wt == 1
                        else "rolling priority"
                    )
                    if _wt == 2 and _s.get("waiver_budget") is not None:
                        try:
                            meta.waiver_budget = int(_s["waiver_budget"])
                        except (TypeError, ValueError):
                            pass
        except Exception as exc:
            logger.warning("Sleeper league metadata fetch failed: %s", exc)
        try:
            drafts = await self._get(f"/league/{self._league.league_id}/drafts")
            if isinstance(drafts, list) and drafts:
                draft = drafts[0]
                dtype = str(draft.get("type", "")).lower()
                meta.draft_type = "auction" if dtype == "auction" else "snake"
                # Explicit draft status (pre_draft | drafting | complete) — the
                # undrafted signal for Sleeper (whose draft_date is often null).
                status = str(draft.get("status", "")).strip().lower()
                if status:
                    meta.draft_status = status
                start_ms = draft.get("start_time")
                if start_ms:
                    meta.draft_date = datetime.fromtimestamp(int(start_ms) / 1000, tz=timezone.utc)
        except Exception as exc:
            logger.warning("Sleeper draft metadata fetch failed: %s", exc)
        return meta

    async def _starting_slot_order(self) -> Optional[list[str]]:
        """The league's STARTING slots, in the order Sleeper indexes them.

        `roster_positions` on the league object is the ordered token list including
        bench entries. Dropping "BN" leaves the starting slots, and index i of that
        list is the slot of index i of a roster's `starters` array — verified live
        across a large sample, with the bench always a contiguous tail and IR/taxi
        never appearing in the list at all.

        None when it cannot be read, so the caller leaves every slot unknown rather
        than mislabelling positions.
        """
        try:
            lg = await self._get(f"/league/{self._league.league_id}")
        except Exception as exc:
            logger.warning("Sleeper league %s: roster_positions fetch failed (%s) — "
                           "lineup slots stay UNKNOWN", self._league.league_id, exc)
            return None
        raw = (lg or {}).get("roster_positions") if isinstance(lg, dict) else None
        if not isinstance(raw, list) or not raw:
            return None
        return [str(p) for p in raw if str(p) != "BN"]

    async def get_rosters(self) -> list[TeamRoster]:
        rosters = await self._get(
            f"/league/{self._league.league_id}/rosters"
        )
        users = await self._get(
            f"/league/{self._league.league_id}/users"
        )
        user_map = {u["user_id"]: u for u in users}
        # One extra request for the whole call, not one per team.
        slot_order = await self._starting_slot_order()

        result: list[TeamRoster] = []
        for roster in rosters:
            user = user_map.get(roster.get("owner_id"), {})
            player_ids = roster.get("players") or []
            # Where the MANAGER has each player seated. starters is slot-ordered and
            # positional: index i is slot_order[i]. Sleeper uses "0" for an empty
            # slot, which holds the index alignment, so it is skipped rather than
            # letting it shift every later slot.
            slot_by_pid: dict[str, str] = {}
            starters = roster.get("starters") or []
            # Per-ROSTER, not per-league: one team with a shape we do not recognise
            # must not make the rest of the league unknown, and must not be labelled.
            slots_known = slot_order is not None
            if slot_order is not None:
                if len(starters) != len(slot_order):
                    # Never guess past a shape we do not recognise. Leaving these
                    # unknown is the point: calling them all "BENCH" would be a
                    # positive claim that every player is benched.
                    logger.warning(
                        "Sleeper league %s roster %s: %d starters but %d starting "
                        "slots — lineup slots left UNKNOWN for this team",
                        self._league.league_id, roster.get("roster_id"),
                        len(starters), len(slot_order),
                    )
                    slots_known = False
                else:
                    for slot, pid in zip(slot_order, starters):
                        if pid and str(pid) != "0":
                            slot_by_pid[str(pid)] = slot
            # Injured reserve is a real lineup placement the manager chose, and it is
            # the strongest availability signal there is — it beats any global feed.
            # It comes from its own array, so it stands even when the starting-slot
            # order could not be read.
            for pid in (roster.get("reserve") or []):
                if pid:
                    slot_by_pid[str(pid)] = "IR"

            players = [
                RosteredPlayer(
                    platform_player_id=pid,
                    player_name="",
                    position="",
                    team_abbr="",
                    # A named slot if we have one. Otherwise "BENCH" ONLY when the
                    # slots were readable and this player simply was not in them;
                    # when they were not readable the answer is None, meaning we do
                    # not know — never "benched".
                    lineup_slot=(slot_by_pid.get(str(pid))
                                 or ("BENCH" if slots_known else None)),
                )
                for pid in player_ids
            ]
            # Owner identity for is_me binding: owner_id + any co_owners (co-owned team).
            owner_ids = [str(roster["owner_id"])] if roster.get("owner_id") else []
            owner_ids += [str(c) for c in (roster.get("co_owners") or [])]
            result.append(TeamRoster(
                platform_team_id=str(roster["roster_id"]),
                manager_name=user.get("display_name", ""),
                # `or {}`, not a .get default: Sleeper returns metadata EXPLICITLY
                # NULL for a user who never set a team name, and `.get("metadata", {})`
                # returns that None rather than the default. The resulting
                # AttributeError killed get_rosters, which is the fail-hard step of
                # sync — so one league-mate with no team name broke the whole connect.
                team_name=(user.get("metadata") or {}).get("team_name", ""),
                players=players,
                # SPENT, not remaining. Sleeper's waiver_budget_used is the amount
                # already spent — verified against live leagues, where the values
                # top out at exactly the league budget. It used to be assigned to a
                # field named faab_remaining, so a team that had spent its whole
                # budget was shown as having all of it left.
                budget_spent=roster.get("settings", {}).get("waiver_budget_used"),
                # Order position for leagues that use priority rather than bidding.
                waiver_position=roster.get("settings", {}).get("waiver_position"),
                wins=roster.get("settings", {}).get("wins", 0),
                losses=roster.get("settings", {}).get("losses", 0),
                owner_ids=owner_ids,
            ))
        return result

    async def get_free_agents(
        self, position: str | None = None
    ) -> list[FreeAgent]:
        # Sleeper doesn't have a free agent endpoint.
        # Derive: all NFL players NOT on any roster.
        return []

    async def get_draft_picks(
        self, *, league_key: str | None = None,
    ) -> list[DraftPick]:
        drafts = await self._get(
            f"/league/{self._league.league_id}/drafts"
        )
        all_picks: list[DraftPick] = []
        for draft in drafts:
            picks = await self._get(
                f"/draft/{draft['draft_id']}/picks"
            )
            for pick in picks:
                metadata = pick.get("metadata", {})
                all_picks.append(DraftPick(
                    platform_player_id=pick.get("player_id", ""),
                    player_name=(
                        f"{metadata.get('first_name', '')} "
                        f"{metadata.get('last_name', '')}"
                    ).strip(),
                    position=metadata.get("position", ""),
                    team_abbr=metadata.get("team", ""),
                    picked_by_team_id=str(pick.get("roster_id", "")),
                    manager_name="",
                    pick_number=pick.get("pick_no", 0),
                    round_number=pick.get("round", 0),
                    auction_price=pick.get("amount"),
                ))
        return all_picks

    async def get_matchups(self, week: int) -> Optional[list[WeeklyMatchup]]:
        """The league's REAL head-to-head schedule for ``week``.

        `/league/{id}/matchups/{week}` returns one entry PER ROSTER, not per game:
        two rosters sharing a matchup_id are playing each other. Verified live. A
        null matchup_id means that roster has no game that week (eliminated from the
        playoff bracket), so it is dropped rather than paired with anything.

        Available BEFORE a week is played, which is what the matchup page needs — it
        is a stored season schedule, not a result feed. It only exists once the
        league has drafted; before that Sleeper returns an empty list.

        None on any failure, so the caller withholds an opponent rather than
        inventing one. An empty list is a real answer: no game this week.
        """
        try:
            rows = await self._get(f"/league/{self._league.league_id}/matchups/{week}")
        except Exception as exc:
            logger.warning(
                "Sleeper league %s: week-%s matchup fetch failed (%s: %s) — reporting "
                "UNKNOWN, not an empty schedule",
                self._league.league_id, week, type(exc).__name__, exc,
            )
            return None
        if not isinstance(rows, list):
            logger.warning("Sleeper league %s: week-%s matchups had unexpected shape %s "
                           "— reporting UNKNOWN", self._league.league_id, week, type(rows).__name__)
            return None

        by_matchup: dict[object, list[dict]] = {}
        no_game = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            mid = row.get("matchup_id")
            if mid is None:
                no_game += 1
                continue
            by_matchup.setdefault(mid, []).append(row)
        if no_game:
            logger.info("Sleeper league %s week %s: %d roster(s) have no game (null "
                        "matchup_id)", self._league.league_id, week, no_game)

        out: list[WeeklyMatchup] = []
        for mid in sorted(by_matchup, key=lambda x: (x is None, str(x))):
            side = by_matchup[mid]
            if len(side) != 2:
                # Never guess a pairing. One entry with a matchup_id is a data
                # oddity, not a game we can name an opponent from.
                logger.warning(
                    "Sleeper league %s week %s: matchup_id %r had %d roster(s), not 2 "
                    "— skipped (an opponent is never inferred)",
                    self._league.league_id, week, mid, len(side),
                )
                continue
            # Sleeper does not designate a home side; order by roster_id so the same
            # week always produces the same pairing.
            a, b = sorted(side, key=lambda r: str(r.get("roster_id")))
            out.append(WeeklyMatchup(
                week=week,
                home_team_id=str(a.get("roster_id")),
                away_team_id=str(b.get("roster_id")),
                home_score=float(a.get("points") or 0.0),
                away_score=float(b.get("points") or 0.0),
                # Sleeper reports points as they accrue and never flags completion,
                # so this stays False: a forward-looking preview, not a result.
                is_complete=False,
            ))
        return out

    async def get_transactions(self, week: int) -> list[Transaction]:
        return []

    async def get_standings(self) -> list[TeamRoster]:
        return await self.get_rosters()
