"""
LeagueConfig — dataclass (NOT a SQLAlchemy model).

Single source of truth for league-specific parameters.
Replaces all hardcoded 12-team / $200 / PPR assumptions.
"""
from dataclasses import dataclass
from typing import Literal


@dataclass
class LeagueConfig:
    # Core settings (user-provided)
    team_count: int = 12
    draft_type: Literal["auction", "snake"] = "auction"
    scoring: Literal["ppr", "half_ppr"] = "ppr"
    budget: int = 200                    # auction only
    pick_position: int | None = None     # snake only (1-N)
    platform: Literal["yahoo", "espn", "sleeper"] = "yahoo"
    league_id: str = ""
    season_year: int = 2026

    # Roster slots (standard for all supported leagues)
    # Non-standard leagues (2QB, superflex, IDP) not supported
    qb_slots: int = 1
    rb_slots: int = 2
    wr_slots: int = 2
    te_slots: int = 1
    flex_slots: int = 1          # RB/WR/TE
    k_slots: int = 1
    def_slots: int = 1
    bench_slots: int = 7

    # Derived — computed from above, never set directly
    @property
    def total_teams(self) -> int:
        return self.team_count

    @property
    def skill_starter_slots(self) -> int:
        """QB+RB+WR+TE+FLEX — the $185 target positions"""
        return (self.qb_slots + self.rb_slots +
                self.wr_slots + self.te_slots + self.flex_slots)

    @property
    def skill_budget_pct(self) -> float:
        """Fraction of budget for skill starters"""
        # K + DEF + bench = low value, ~$15 of $200
        return 0.925  # consistent across league sizes

    @property
    def total_skill_pool(self) -> float:
        """Total auction dollars across all teams for skill positions"""
        return self.budget * self.team_count * self.skill_budget_pct

    @property
    def wr_replacement_rank(self) -> int:
        """WR rank below which player has no surplus value"""
        # 2 WR starters + 0.6 flex share per team
        return round((self.wr_slots + 0.6) * self.team_count)

    @property
    def rb_replacement_rank(self) -> int:
        return round((self.rb_slots + 0.4) * self.team_count)

    @property
    def qb_replacement_rank(self) -> int:
        return round(self.qb_slots * self.team_count * 1.2)

    @property
    def te_replacement_rank(self) -> int:
        return round(self.te_slots * self.team_count * 1.3)

    @property
    def is_auction(self) -> bool:
        return self.draft_type == "auction"

    @property
    def is_snake(self) -> bool:
        return self.draft_type == "snake"

    @property
    def rec_points(self) -> float:
        """Points per reception for scoring calculations"""
        return {"ppr": 1.0, "half_ppr": 0.5}.get(self.scoring, 1.0)

    def positional_budget_pct(self, position: str) -> float:
        """Fraction of skill pool allocated to each position"""
        return {
            "RB": 0.38,
            "WR": 0.32,
            "QB": 0.10,
            "TE": 0.10,
            "K":  0.05,
            "DEF": 0.05,
        }.get(position, 0.0)

    def positional_budget(self, position: str) -> float:
        return self.total_skill_pool * self.positional_budget_pct(position)

    # Tier count targets scale with league size
    @property
    def tier_counts(self) -> dict[str, dict[int, int]]:
        scale = self.team_count / 12  # 1.0 for 12-team
        return {
            "WR": {
                1: max(2, round(3 * scale)),
                2: max(4, round(6 * scale)),
                3: max(8, round(10 * scale)),
                4: max(12, round(15 * scale)),
            },
            "RB": {
                1: max(2, round(3 * scale)),
                2: max(4, round(6 * scale)),
                3: max(8, round(10 * scale)),
                4: max(10, round(12 * scale)),
            },
            "QB": {
                1: max(1, round(2 * scale)),
                2: max(2, round(4 * scale)),
                3: max(4, round(6 * scale)),
            },
            "TE": {
                1: max(1, round(2 * scale)),
                2: max(2, round(4 * scale)),
                3: max(4, round(6 * scale)),
            },
        }


# Default config (current single-league behavior, unchanged)
DEFAULT_LEAGUE_CONFIG = LeagueConfig(
    team_count=12,
    draft_type="auction",
    scoring="ppr",
    budget=200,
)
