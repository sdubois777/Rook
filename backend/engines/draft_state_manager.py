"""
Live draft state manager — pure Python, no API calls.

Maintains the complete draft state updated after every pick event.
Used by LiveDraftEngine to calculate budget constraints, track rosters,
and identify drafted players for dependency resolution.

File is intentionally named draft_state_manager.py (not draft_state.py)
because backend/models/draft_state.py already exists with the ORM models.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default roster slots per LEAGUE_RULES.md
_DEFAULT_ROSTER_SLOTS: dict[str, int] = {
    "QB": 1, "RB": 2, "WR": 2, "FLEX": 1, "TE": 1,
    "K": 1, "DEF": 1, "BENCH": 7,
}


@dataclass
class LeagueConfig:
    """Runtime league settings — loaded from DB or constructed with defaults."""

    auction_budget: int = 200
    min_bid: int = 1
    team_count: int = 12
    roster_slots: dict[str, int] = field(default_factory=lambda: dict(_DEFAULT_ROSTER_SLOTS))
    draft_type: str = "auction"   # "auction" | "snake"
    scoring_format: str = "ppr"   # "ppr" | "half_ppr" | "standard"

    @property
    def total_roster_size(self) -> int:
        """Derived from roster_slots to avoid the seeded column bug (15 vs 16)."""
        return sum(self.roster_slots.values())


# Generic self label used until (and unless) a resolver streams the user's real
# own-team name. The team name is COSMETIC — attribution rides on the is_yours
# flag, never this label — so a generic value must never block or misattribute.
DEFAULT_TEAM_LABEL = "Your Team"


def _pick_key(name: str | None) -> str:
    """Strong cross-source name key for pick dedupe: suffixes stripped, hyphens
    to spaces, periods/apostrophes dropped ("D.J. Moore" == "DJ Moore",
    "Amon-Ra St. Brown" == "Amon Ra St Brown"). Mirrors the frontend's
    normalizeName so both layers agree on what "the same player" means."""
    import re

    n = (name or "").lower()
    n = re.sub(r"\s+(jr\.?|sr\.?|ii|iii|iv|v)$", "", n)
    n = n.replace("-", " ")
    n = re.sub(r"[.'’]", "", n)
    return re.sub(r"\s+", " ", n).strip()


@dataclass
class DraftPick:
    """Immutable record of a single draft pick."""

    player_id: str        # yahoo_player_id
    team_id: str          # yahoo_team_id of the drafting team
    price: int
    player_name: str = ""
    position: str = ""
    tier: int | None = None


class DraftStateManager:
    """
    Maintains live draft state updated after every pick.

    Pure Python — no API calls, no DB writes.
    All methods are O(1) or O(n) where n is the number of picks so far.
    """

    @classmethod
    def config_from_user_league(
        cls,
        league: "UserLeague | None",
    ) -> LeagueConfig:
        """Build draft LeagueConfig from a user's connected league.

        Falls back to defaults if no league provided.
        """
        if league is None:
            return LeagueConfig()

        budget = league.budget or 200
        draft_type = league.draft_type or "auction"
        team_count = league.team_count or 12

        # Per-league lineup (T3): use the league's normalized roster_slots when
        # present, else the default. Null → byte-unchanged default config.
        league_slots = getattr(league, "roster_slots", None)
        roster_slots = dict(league_slots) if league_slots else dict(_DEFAULT_ROSTER_SLOTS)

        return LeagueConfig(
            auction_budget=budget if draft_type == "auction" else 0,
            min_bid=1,
            team_count=team_count,
            roster_slots=roster_slots,
            draft_type=draft_type,
            scoring_format=getattr(league, "scoring", None) or "ppr",
        )

    def __init__(self, league_config: LeagueConfig, your_team_id: str = ""):
        self.league_config = league_config
        # Display label for your team. Defaults to the generic label; upgraded to
        # the real name via set_your_team_name() when a resolver streams one.
        self.your_team_id = your_team_id or DEFAULT_TEAM_LABEL

        self.picks: list[DraftPick] = []
        self.opponent_rosters: dict[str, list[DraftPick]] = {}
        self.your_roster: list[DraftPick] = []
        self.opponent_budgets: dict[str, int] = {}
        self.your_budget: int = league_config.auction_budget

        # Your most recent bid, captured from the extension's `my_bid` relay
        # ({player_id, amount}). Used to recover a sale whose winner the DOM
        # poller couldn't attribute (winner='unknown'). None until you bid.
        self.last_my_bid: dict | None = None

        # Normalized names of every drafted player (snake). The snake_pick
        # player_id is a Yahoo-internal id that doesn't match our DB
        # yahoo_player_id, so the engine excludes drafted players from
        # recommendations by NAME instead. See is_drafted().
        self._drafted_names: set[str] = set()

        # YOUR snake picks, tracked from the snake_pick is_yours flag. Can't use
        # your_roster here: snake picks arrive with team_id="You" (the Picks-panel
        # label), which never equals your_team_id (your team name), so record_pick
        # never files them. The is_yours flag is the reliable signal.
        self._my_picks: list[dict] = []

    def set_your_team_name(self, name: str | None) -> bool:
        """Upgrade the generic label to the real own-team name a resolver derived
        (ESPN). Idempotent + non-destructive: only upgrades FROM the generic
        default, so a real name already set is never clobbered by a later frame,
        and a missing/blank name is a no-op. Returns True if the label changed.

        Purely cosmetic — never affects pick attribution (that is is_yours)."""
        if not name or not name.strip():
            return False
        name = name.strip()
        if self.your_team_id and self.your_team_id != DEFAULT_TEAM_LABEL:
            return False  # a real name is already set — keep it
        if self.your_team_id == name:
            return False
        self.your_team_id = name
        return True

    def record_snake_pick(
        self,
        player_name: str,
        position: str | None = None,
        pick_number: int | None = None,
        round_num: int | None = None,
        is_yours: bool = False,
    ) -> bool:
        """Track a snake pick: add its name to the drafted set (for exclusion),
        and — when it's yours — append it to your roster (for recommendations).

        IDEMPOTENT: a re-relayed pick (extension reload re-scan, page refresh,
        state backfill) is a no-op — a player can only be drafted once per draft,
        so a name already in the drafted set never double-books _my_picks.
        Returns True when the pick was NEWLY recorded, False on a duplicate.
        """
        if player_name and self.is_drafted(player_name):
            return False
        if player_name:
            from backend.agents.roster_changes import _norm_name

            self._drafted_names.add(_norm_name(player_name))
        if is_yours and player_name:
            self._my_picks.append({
                "player_name": player_name,
                "position": position,
                "pick_number": pick_number,
                "round": round_num,
            })
        return bool(player_name)

    def get_my_roster(self) -> list[dict]:
        """Your snake picks in draft order (player_name/position/pick_number/round)."""
        return self._my_picks.copy()

    def get_unfilled_needs(self, roster: list[dict]) -> dict[str, int]:
        """STRUCTURED unfilled starter slots — {pos_or_slot: count} (the single
        needs implementation; format_roster_needs renders it, the deterministic
        pick logic consumes it).

        Driven by the league's real roster_slots (T3): a no-DEF league never wants
        a DEF, a superflex league wants a QB-eligible flex, the bench count is the
        league's own. FLEX is filled by surplus RB/WR/TE beyond their fixed slots;
        SUPER_FLEX additionally admits QB. BENCH/IR/UNSUPPORTED are depth, not needs.
        Keys are positions ("RB") plus flex slot types ("FLEX", "SUPER_FLEX").
        """
        from backend.services.roster_slots import FLEX_ELIGIBLE

        slots = self.league_config.roster_slots or {}
        filled: dict[str, int] = {}
        for pick in roster:
            pos = (pick.get("position") or "BN").upper()
            filled[pos] = filled.get(pos, 0) + 1

        needs: dict[str, int] = {}
        # Fixed starter slots — only those the league actually has.
        for pos in ("QB", "RB", "WR", "TE", "K", "DEF"):
            req = int(slots.get(pos, 0) or 0)
            have = filled.get(pos, 0)
            if have < req:
                needs[pos] = req - have

        # Surplus beyond the fixed requirements feeds the flex slots.
        surplus = {p: max(0, filled.get(p, 0) - int(slots.get(p, 0) or 0))
                   for p in ("QB", "RB", "WR", "TE")}
        for slot_type in ("FLEX", "SUPER_FLEX"):
            n = int(slots.get(slot_type, 0) or 0)
            elig = FLEX_ELIGIBLE.get(slot_type, ())
            for _ in range(n):
                if sum(surplus.get(p, 0) for p in elig) >= 1:
                    take = max(elig, key=lambda p: surplus.get(p, 0))
                    surplus[take] -= 1
                else:
                    needs[slot_type] = needs.get(slot_type, 0) + 1
        return needs

    def need_positions(self, roster: list[dict]) -> set[str]:
        """Concrete positions that still fill a starter slot (flex expanded to
        its eligible positions)."""
        from backend.services.roster_slots import FLEX_ELIGIBLE

        out: set[str] = set()
        for key in self.get_unfilled_needs(roster):
            if key in FLEX_ELIGIBLE:
                out.update(FLEX_ELIGIBLE[key])
            else:
                out.add(key)
        return out

    def format_roster_needs(self, roster: list[dict]) -> str:
        """Human-readable rendering of get_unfilled_needs() (single source)."""
        from backend.services.roster_slots import FLEX_ELIGIBLE

        needs = self.get_unfilled_needs(roster)
        lines: list[str] = []
        for key, n in needs.items():
            if key in FLEX_ELIGIBLE:
                label = "/".join(FLEX_ELIGIBLE[key])
                lines.extend([f"{key}: 1 more ({label})"] * n)
            else:
                lines.append(f"{key}: need {n} more")
        return "\n".join(lines) if lines else "All starters filled — draft for depth/upside"

    def is_drafted(self, player_name: str) -> bool:
        """True if this player has already been drafted.

        Matches on the normalized name, and is abbreviation-aware (the snake DOM
        sends "J. Gibbs" but the recommendation pool has "Jahmyr Gibbs"): a
        same-last-name + same-first-initial match counts, in either direction.
        """
        from backend.agents.roster_changes import _norm_name

        key = _norm_name(player_name or "")
        if not key:
            return False
        if key in self._drafted_names:
            return True

        parts = key.split()
        if len(parts) < 2:
            return False
        last, initial = parts[-1], parts[0][:1]
        for drafted in self._drafted_names:
            dp = drafted.split()
            if len(dp) >= 2 and dp[-1] == last and dp[0][:1] == initial:
                return True
        return False

    def record_my_bid(self, player_id: str, amount: int) -> None:
        """Remember your latest bid so an unattributed sale can be recovered."""
        self.last_my_bid = {"player_id": player_id or "", "amount": amount}

    def is_my_winning_bid(self, player_id: str, final_price: int) -> bool:
        """True if your last recorded bid won this sale.

        The DOM poller attributes a sale by budget/slot delta, which fails when
        the room's team panel hasn't updated yet (winner='unknown'). In that
        case, if your last bid matches the final price — and the player id too,
        when both are known — the player is yours. Matches by price alone only
        when the sold player's id couldn't be resolved.
        """
        bid = self.last_my_bid
        if not bid:
            return False
        if bid.get("amount") != final_price:
            return False
        bid_pid = bid.get("player_id") or ""
        if bid_pid and player_id and bid_pid != player_id:
            return False
        return True

    # --- League type accessors (drive the snake vs auction engine path) ---

    @property
    def draft_type(self) -> str:
        return self.league_config.draft_type

    @property
    def scoring_format(self) -> str:
        return self.league_config.scoring_format

    @property
    def is_snake(self) -> bool:
        return self.league_config.draft_type == "snake"

    @property
    def is_auction(self) -> bool:
        return self.league_config.draft_type == "auction"

    def reconcile_draft_type(self, target: str) -> bool:
        """Reconcile this session's format to the LIVE-detected draft type.

        The live draft is the single source of truth for format (the extension
        detects it at frame 0). A stale session inside the resume window can hold
        the WRONG format and mis-route recommendations — the reported case is a
        snake session mis-routing an auction draft, but the SYMMETRIC case (an
        auction session mis-routing a snake draft) is equally real. Reconcile in
        BOTH directions. Switching to auction restores the budget a snake config
        zeroes, so the auction recommendation has a real budget to reason with.

        Called on both freshly-built AND rehydrated/resumed sessions — the bug
        lives specifically in the resume path, so setting format only on create
        would miss it. Returns True if the format actually changed.
        """
        if target not in ("auction", "snake"):
            return False
        cfg = self.league_config
        if cfg.draft_type == target:
            return False
        cfg.draft_type = target
        # A format change means the resume window matched a DIFFERENT draft, so
        # the auction budget from a snake config (0) must be restored.
        if target == "auction" and cfg.auction_budget <= 0:
            cfg.auction_budget = LeagueConfig().auction_budget
            self.your_budget = cfg.auction_budget
        return True

    def get_roster_summary(self) -> dict[str, list[dict]]:
        """Your roster grouped by position — for snake roster-need reasoning.

        { "RB": [{"player_name": ..., "price": ...}], "WR": [...], ... }
        Price is included but irrelevant in snake; kept for a uniform shape.
        """
        summary: dict[str, list[dict]] = {}
        for pick in self.your_roster:
            pos = (pick.position or "UNK").upper()
            summary.setdefault(pos, []).append(
                {"player_name": pick.player_name, "price": pick.price}
            )
        return summary

    def record_pick(self, pick: DraftPick, is_yours: bool = False) -> bool:
        """Called after every draft_pick event from the bridge.

        `is_yours` (the extension's own-pick flag) routes the pick to YOUR roster
        even when team_id is an anonymous slot label ("Team 5") that doesn't match
        your_team_id — without it, Sleeper/ESPN buys landed in opponent_rosters
        under your own slot, and a refresh showed them there with your roster empty.

        IDEMPOTENT: a re-relayed sale (extension reload re-scans the full board,
        page refresh, state backfill) must not double-charge budgets or duplicate
        rosters — a player can only be sold once per draft. Matched by player_id
        when both sides have one, else by normalized name. Returns True when the
        pick was NEWLY recorded, False on a duplicate.
        """
        new_key = _pick_key(pick.player_name) or None
        for p in self.picks:
            if pick.player_id and p.player_id and p.player_id == pick.player_id:
                return False
            if new_key and p.player_name and _pick_key(p.player_name) == new_key:
                return False

        self.picks.append(pick)

        if is_yours or (pick.team_id and pick.team_id == self.your_team_id):
            self.your_roster.append(pick)
            self.your_budget -= pick.price
        else:
            self.opponent_rosters.setdefault(pick.team_id, []).append(pick)
            self.opponent_budgets[pick.team_id] = (
                self.opponent_budgets.get(
                    pick.team_id, self.league_config.auction_budget
                )
                - pick.price
            )
        return True

    def get_drafted_player_ids(self) -> set[str]:
        """All player_ids that have been drafted so far."""
        return {p.player_id for p in self.picks}

    def get_your_remaining_budget(self) -> int:
        """Your remaining auction budget."""
        return self.your_budget

    def get_roster_slots_remaining(self) -> int:
        """How many roster slots you still need to fill."""
        return self.league_config.total_roster_size - len(self.your_roster)

    def get_minimum_completion_budget(self) -> int:
        """Minimum $1 per remaining roster slot (including current)."""
        return self.get_roster_slots_remaining() * self.league_config.min_bid

    def get_spendable_on_this_player(self) -> int:
        """Maximum you can bid on the current nomination and still complete your roster."""
        return max(0, self.your_budget - self.get_minimum_completion_budget())

    def get_your_positional_counts(self) -> dict[str, int]:
        """Count of players at each position in your roster."""
        counts: dict[str, int] = {}
        for pick in self.your_roster:
            counts[pick.position] = counts.get(pick.position, 0) + 1
        return counts

    # --- Serialization (durability: snapshot to / rehydrate from the DB) ---

    def to_dict(self) -> dict:
        """Serialize the full mutable draft state to a JSON-safe snapshot.

        Captures everything needed to rebuild an identical state after a process
        restart (Railway redeploy) or, in a future multi-worker setup, on a
        different worker. The engine itself is NOT serialized — it is rebuilt and
        this state is reattached via from_dict(). Round-trips exactly:
        from_dict(to_dict()) reproduces all rosters, budgets, picks, and the
        drafted-name set that drive recommendations.
        """
        from dataclasses import asdict

        return {
            "league_config": asdict(self.league_config),
            "your_team_id": self.your_team_id,
            "your_budget": self.your_budget,
            "picks": [asdict(p) for p in self.picks],
            "your_roster": [asdict(p) for p in self.your_roster],
            "opponent_rosters": {
                tid: [asdict(p) for p in roster]
                for tid, roster in self.opponent_rosters.items()
            },
            "opponent_budgets": dict(self.opponent_budgets),
            "last_my_bid": self.last_my_bid,
            # set -> sorted list for JSON; order is irrelevant (membership only).
            "drafted_names": sorted(self._drafted_names),
            "my_picks": list(self._my_picks),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DraftStateManager":
        """Rebuild a DraftStateManager from a to_dict() snapshot.

        Inverse of to_dict(): reconstructs the LeagueConfig, DraftPick records,
        rosters, budgets, the drafted-name set, and your snake picks.
        """
        cfg = dict(data.get("league_config") or {})
        # roster_slots round-trips as a plain dict; LeagueConfig accepts it.
        league_config = LeagueConfig(**cfg) if cfg else LeagueConfig()

        state = cls(league_config, data.get("your_team_id", "") or "")
        state.your_budget = data.get("your_budget", league_config.auction_budget)
        state.picks = [DraftPick(**p) for p in (data.get("picks") or [])]
        state.your_roster = [DraftPick(**p) for p in (data.get("your_roster") or [])]
        state.opponent_rosters = {
            tid: [DraftPick(**p) for p in roster]
            for tid, roster in (data.get("opponent_rosters") or {}).items()
        }
        state.opponent_budgets = dict(data.get("opponent_budgets") or {})
        state.last_my_bid = data.get("last_my_bid")
        state._drafted_names = set(data.get("drafted_names") or [])
        state._my_picks = list(data.get("my_picks") or [])
        return state
