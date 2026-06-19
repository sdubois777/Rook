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

        return LeagueConfig(
            auction_budget=budget if draft_type == "auction" else 0,
            min_bid=1,
            team_count=team_count,
            draft_type=draft_type,
            scoring_format=getattr(league, "scoring", None) or "ppr",
        )

    def __init__(self, league_config: LeagueConfig, your_team_id: str):
        self.league_config = league_config
        self.your_team_id = your_team_id

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

    def record_snake_pick(self, player_name: str) -> None:
        """Track a drafted player's name so it's excluded from recommendations."""
        if player_name:
            from backend.agents.roster_changes import _norm_name

            self._drafted_names.add(_norm_name(player_name))

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

    def record_pick(self, pick: DraftPick) -> None:
        """Called after every draft_pick event from the bridge."""
        self.picks.append(pick)

        if pick.team_id == self.your_team_id:
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
