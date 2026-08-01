"""
Shared data models for platform API responses.
All three platforms (Yahoo, ESPN, Sleeper) map their
responses to these models before returning.

Agents and services work with these models exclusively —
never with raw platform API responses.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# The waiver_system label for a league that runs NO waiver process at all — every
# drop is immediately available. Such a league has neither a bidding budget nor a
# waiver order, so this value is the signal to withhold both. Shared rather than
# written as a literal in two places, because a typo in either would silently
# re-enable the claim it exists to suppress.
NO_WAIVERS_SYSTEM = "free agency, no waivers"


@dataclass
class LeagueMetadata:
    """Pre-draft league metadata, mapped from whatever call a platform already makes.
    Every field Optional — a platform fills what its response exposes; None means
    "not available, don't overwrite". draft_date is tz-aware UTC."""
    name: Optional[str] = None
    scoring: Optional[str] = None          # ppr | half_ppr | standard
    team_count: Optional[int] = None
    draft_type: Optional[str] = None       # auction | snake
    draft_date: Optional[datetime] = None
    draft_status: Optional[str] = None     # pre_draft | drafting | complete (Sleeper); None elsewhere

    # --- waiver settings -----------------------------------------------------
    # uses_bidding_budget is the ONLY field that decides whether a dollar bid is
    # meaningful. Do NOT infer it from waiver_budget: every platform ships a
    # vestigial budget value on leagues that do not bid at all. Measured across
    # 285 live Sleeper leagues, waiver_budget was present on 285 of 285 including
    # every rolling-waiver league, almost always as 100 — which is exactly the
    # fabricated figure this field exists to stop us showing.
    #   None  = could not determine; caller must not claim either way
    #   True  = league bids with a budget
    #   False = league uses priority or reverse standings; a dollar bid is meaningless
    uses_bidding_budget: Optional[bool] = None
    waiver_budget: Optional[int] = None    # only meaningful when uses_bidding_budget is True
    waiver_system: Optional[str] = None    # short human label, e.g. "budget", "rolling priority"


@dataclass
class RosteredPlayer:
    """A player on a fantasy team's roster."""
    platform_player_id: str
    player_name: str
    position: str           # QB, RB, WR, TE, K, DEF
    team_abbr: str          # NFL team
    is_starter: bool = False
    injury_status: Optional[str] = None
    # full | questionable | doubtful | out | None


@dataclass
class TeamRoster:
    """One fantasy team's full roster."""
    platform_team_id: str
    manager_name: str
    team_name: str
    players: list[RosteredPlayer] = field(default_factory=list)
    # What the team has SPENT from a bidding budget. Named for what it holds:
    # both Sleeper and ESPN report spend, not remaining balance, and the previous
    # field was named faab_remaining while being assigned the spent amount — so
    # a team that had spent everything appeared to have everything left.
    # Remaining is computed downstream, where the league budget is known.
    budget_spent: Optional[int] = None
    # Waiver order position for leagues that use priority instead of bidding.
    waiver_position: Optional[int] = None
    wins: int = 0
    losses: int = 0
    points_for: float = 0.0
    # OWNER IDENTITY for exact is_me binding (never name/position). Sleeper: [owner_id,
    # *co_owners]; ESPN: the SWID owners[] list (all owners, not just primary). Matched
    # against the user's stored platform identity.
    owner_ids: list[str] = field(default_factory=list)
    # Server-tagged "this is the authed user's team" (Yahoo is_owned_by_current_login).
    # None = platform doesn't tag it (bind via owner_ids instead).
    is_me: Optional[bool] = None


@dataclass
class FreeAgent:
    """An unowned player available on waiver wire."""
    platform_player_id: str
    player_name: str
    position: str
    team_abbr: str
    ownership_pct: float = 0.0
    waiver_priority: Optional[int] = None


@dataclass
class DraftPick:
    """A single pick from a completed draft."""
    platform_player_id: str
    player_name: str
    position: str
    team_abbr: str
    picked_by_team_id: str
    manager_name: str
    pick_number: int
    round_number: int
    auction_price: Optional[int] = None  # None for snake drafts


@dataclass
class WeeklyMatchup:
    """One matchup between two teams for a week."""
    week: int
    home_team_id: str
    away_team_id: str
    home_score: float
    away_score: float
    is_complete: bool


@dataclass
class Transaction:
    """A waiver claim, trade, or free agent add."""
    type: str               # add | drop | trade
    player_name: str
    position: str
    added_by_team_id: Optional[str] = None
    dropped_by_team_id: Optional[str] = None
    week: int = 0
    faab_bid: Optional[int] = None
