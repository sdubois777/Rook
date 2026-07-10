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
    faab_remaining: Optional[int] = None
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
