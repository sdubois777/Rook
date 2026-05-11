"""
League settings table — single row per league.

All constants from docs/rules/LEAGUE_RULES.md are stored here.
Application code must load from this table — never hardcode values.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class LeagueSettings(Base):
    __tablename__ = "league_settings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Basic league config
    platform: Mapped[str] = mapped_column(String(50), nullable=False, default="Yahoo")
    scoring_format: Mapped[str] = mapped_column(String(10), nullable=False, default="PPR")
    team_count: Mapped[int] = mapped_column(Integer, nullable=False, default=12)

    # Auction budget
    auction_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    min_bid: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Skill starter budget — used for valuation calibration
    # $185 × 12 teams = $2,220 total skill position pool
    skill_starter_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=185)
    league_skill_dollar_pool: Mapped[int] = mapped_column(Integer, nullable=False, default=2220)

    # Roster construction
    total_roster_size: Mapped[int] = mapped_column(Integer, nullable=False, default=16)
    starting_lineup_size: Mapped[int] = mapped_column(Integer, nullable=False, default=9)

    # Roster slots as JSONB: {"QB": 1, "RB": 2, "WR": 2, "FLEX": 1, "TE": 1, "K": 1, "DEF": 1, "BENCH": 7}
    roster_slots: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Positional budget allocations (% of skill_starter_budget × team_count)
    # {"RB": 0.38, "WR": 0.32, "QB": 0.10, "TE": 0.10}
    positional_budget_pcts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Replacement-level PPR points per game by position
    # {"QB": 18.0, "RB": 8.0, "WR": 7.0, "TE": 5.0}
    replacement_level_ppr: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Maximum realistic bid per position — any ceiling above $80 is a bug
    # {"RB": 80, "WR": 70, "QB": 50, "TE": 45, "K": 2, "DEF": 2}
    max_realistic_bid: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Typical bid ranges per position tier
    # {"RB1": [50, 75], "RB2": [20, 40], "WR1": [40, 60], ...}
    typical_bid_ranges: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
